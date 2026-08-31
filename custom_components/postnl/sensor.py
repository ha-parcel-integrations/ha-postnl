"""Sensor platform for the PostNL integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PostNLConfigEntry
from .const import DOMAIN, ParcelStatus
from .coordinator import PostNLCoordinator
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out; HA's per-entity throttling adds nothing.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostNLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PostNL sensor entities from a config entry."""
    data = entry.runtime_data
    userinfo: dict[str, Any] = data.userinfo
    account_id: str = userinfo.get("account_id", "")

    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = data.coordinator

    receiver_parcels: list[dict] = _active_receiver(coordinator)
    current_barcodes: set[str] = {p["barcode"] for p in receiver_parcels if p.get("barcode")}

    # Remove stale per-parcel sensors that are no longer active.
    registry = er.async_get(hass)
    non_parcel_unique_ids = {
        f"{account_id}_incoming_parcels",
        f"{account_id}_awaiting_pickup",
        f"{account_id}_next_delivery",
        f"{account_id}_en_route_to_service_point",
        f"{account_id}_outgoing_parcels",
        f"{account_id}_delivered_parcels",
        f"{account_id}_outgoing_delivered_parcels",
        f"{account_id}_letters",
        f"{account_id}_last_update",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        # Only per-parcel *sensors* are managed here; skip other platforms
        # (the refresh button, letter images) whose unique_id also starts
        # with account_id_.
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{account_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            barcode = entity_entry.unique_id[len(f"{account_id}_"):]
            if barcode not in current_barcodes:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        PostNLIncomingParcelsSensor(
            coordinator=coordinator,
            userinfo=userinfo,
            async_add_entities=async_add_entities,
            known_barcodes=current_barcodes,
        ),
        PostNLAwaitingPickupSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLNextDeliverySensor(coordinator=coordinator, userinfo=userinfo),
        PostNLEnRouteToServicePointSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLOutgoingParcelsSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLDeliveredParcelsSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLOutgoingDeliveredParcelsSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLLettersSensor(coordinator=coordinator, userinfo=userinfo),
        PostNLLastUpdateSensor(coordinator=coordinator, userinfo=userinfo),
    ]

    for parcel in receiver_parcels:
        if parcel.get("barcode"):
            entities.append(PostNLParcelSensor(coordinator=coordinator, userinfo=userinfo, barcode=parcel["barcode"]))

    async_add_entities(entities)




def _active_receiver(coordinator: PostNLCoordinator) -> list[dict]:
    """Return non-delivered receiver parcels."""
    return [p for p in (coordinator.data or {}).get("receiver", []) if not p.get("delivered")]


class PostNLIncomingParcelsSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Summary sensor for active incoming PostNL parcels.

    Spawns a per-parcel :class:`PostNLParcelSensor` whenever a new barcode
    appears, and removes the per-parcel sensor from the entity registry
    when its barcode drops out of the coordinator data. Doing the removal
    here (synchronously, via the registry) instead of having the per-parcel
    sensor self-remove from inside its own ``_handle_coordinator_update``
    avoids the race where ``async_remove(force_remove=True)`` competes with
    the coordinator-listener cleanup and leaves a ghost entity behind.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
        async_add_entities: AddEntitiesCallback,
        known_barcodes: set[str] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._userinfo = userinfo
        self._async_add_entities = async_add_entities
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_incoming_parcels"
        self._attr_device_info = build_device_info(userinfo)
        self._known_barcodes: set[str] = known_barcodes or set()

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(_active_receiver(self.coordinator))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": _active_receiver(self.coordinator)}

    def _handle_coordinator_update(self) -> None:
        current_barcodes = {
            p["barcode"]
            for p in _active_receiver(self.coordinator)
            if p.get("barcode")
        }

        new_barcodes = current_barcodes - self._known_barcodes
        if new_barcodes:
            self._async_add_entities(
                PostNLParcelSensor(coordinator=self.coordinator, userinfo=self._userinfo, barcode=b)
                for b in new_barcodes
            )

        removed_barcodes = self._known_barcodes - current_barcodes
        if removed_barcodes:
            registry = er.async_get(self.hass)
            account_id: str = self._userinfo.get("account_id", "")
            for barcode in removed_barcodes:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{account_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_barcodes = current_barcodes
        super()._handle_coordinator_update()


class PostNLAwaitingPickupSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Parcels that have arrived at a pickup point and are ready to collect."""

    _attr_has_entity_name = True
    _attr_translation_key = "awaiting_pickup"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: PostNLCoordinator, userinfo: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_awaiting_pickup"
        self._attr_device_info = build_device_info(userinfo)

    def _parcels(self) -> list[dict]:
        return [
            parcel
            for parcel in _active_receiver(self.coordinator)
            if parcel.get("pickup")
            and parcel.get("status") == ParcelStatus.AT_PICKUP_POINT
        ]

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._parcels()}


class PostNLParcelSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Per-parcel sensor for a single active incoming PostNL shipment."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcel"
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"raw", "history"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
        barcode: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._barcode = barcode
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}
        self._attr_device_info = build_device_info(userinfo)

    def _get_parcel(self) -> dict | None:
        for parcel in _active_receiver(self.coordinator):
            if parcel.get("barcode") == self._barcode:
                return parcel
        return None

    @property
    def native_value(self) -> str | None:
        """Return the native value of the sensor."""
        parcel = self._get_parcel()
        return parcel.get("status") if parcel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}



class PostNLNextDeliverySensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Sensor reporting the earliest expected delivery datetime across all active incoming parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = "Data provided by PostNL"

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_next_delivery"
        self._attr_device_info = build_device_info(userinfo)

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        for parcel in _active_receiver(self.coordinator):
            moment_str = parcel.get("planned_from")
            if not moment_str:
                continue
            try:
                dt = datetime.fromisoformat(moment_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result.append((dt, parcel))
            except ValueError:
                _LOGGER.debug("Could not parse delivery moment: %s", moment_str)
        return result

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
        }


class PostNLEnRouteToServicePointSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Sensor reporting active incoming parcels destined for a PostNL point."""

    _attr_has_entity_name = True
    _attr_translation_key = "en_route_to_service_point"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_en_route_to_service_point"
        self._attr_device_info = build_device_info(userinfo)

    def _get_service_point_parcels(self) -> list[dict]:
        return [p for p in _active_receiver(self.coordinator) if p.get("pickup")]

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._get_service_point_parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._get_service_point_parcels()}


class PostNLOutgoingParcelsSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Summary sensor for active outgoing PostNL shipments."""

    _attr_has_entity_name = True
    _attr_translation_key = "outgoing_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_outgoing_parcels"
        self._attr_device_info = build_device_info(userinfo)

    def _active_sender(self) -> list[dict]:
        return [p for p in (self.coordinator.data or {}).get("sender", []) if not p.get("delivered")]

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._active_sender())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._active_sender()}


class PostNLDeliveredParcelsSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Sensor reporting recently delivered incoming PostNL parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_delivered_parcels"
        self._attr_device_info = build_device_info(userinfo)

    @property
    def _parcels(self) -> list[dict]:
        return self.coordinator.delivered_receiver or []

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._parcels)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._parcels}


class PostNLOutgoingDeliveredParcelsSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Sensor reporting recently delivered outgoing PostNL parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "outgoing_delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_outgoing_delivered_parcels"
        self._attr_device_info = build_device_info(userinfo)

    @property
    def _parcels(self) -> list[dict]:
        return self.coordinator.delivered_sender or []

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._parcels)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._parcels}


class PostNLLettersSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Sensor reporting letters announced by PostNL's MyMail service."""

    _attr_has_entity_name = True
    _attr_translation_key = "letters"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by PostNL"
    _unrecorded_attributes = frozenset({"letters"})

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_letters"
        self._attr_device_info = build_device_info(userinfo)

    @property
    def _letters(self) -> list[dict]:
        return self.coordinator.letters or []

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._letters)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {
            "unread": sum(1 for letter in self._letters if letter.get("unread")),
            "letters": self._letters,
        }


class PostNLLastUpdateSensor(CoordinatorEntity[PostNLCoordinator], SensorEntity):
    """Diagnostic sensor reporting when PostNL was last polled successfully.

    Updates on every successful coordinator refresh, even when no parcel
    value changes — so users can alert on a silently-stale integration
    (e.g. expired auth) that the count sensors would not reveal.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = "Data provided by PostNL"

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        userinfo: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_last_update"
        self._attr_device_info = build_device_info(userinfo)

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the last successful poll."""
        return self.coordinator.last_success_time

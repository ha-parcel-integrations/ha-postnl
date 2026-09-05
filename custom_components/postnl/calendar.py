"""Calendar platform for the PostNL integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import PostNLConfigEntry
from .coordinator import PostNLCoordinator
from .device import ATTRIBUTION, build_device_info

# The coordinator fans data out to this entity; no per-entity polling.
PARALLEL_UPDATES = 0

# Fallback window length for a parcel that has a start moment but no end.
_DEFAULT_EVENT_DURATION = timedelta(hours=1)




def _parse(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a timezone-aware datetime, or ``None``."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostNLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PostNL deliveries calendar from a config entry."""
    data = entry.runtime_data
    async_add_entities(
        [PostNLDeliveriesCalendar(data.coordinator, data.userinfo)]
    )


class PostNLDeliveriesCalendar(
    CoordinatorEntity[PostNLCoordinator], CalendarEntity
):
    """A read-only calendar of expected PostNL deliveries.

    Each active incoming parcel with a known delivery moment becomes an
    event. The window is the parcel's ``planned_from``/``planned_to``; when
    only a single moment is known the event is given a one-hour duration.
    No extra API calls — this is purely a view over coordinator data, so it
    is enabled by default and can be turned off per entity if unwanted.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(
        self, coordinator: PostNLCoordinator, userinfo: dict[str, Any]
    ) -> None:
        """Initialise the deliveries calendar."""
        super().__init__(coordinator)
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_deliveries"
        self._attr_device_info = build_device_info(userinfo)

    def _events(self) -> list[CalendarEvent]:
        """Build calendar events from the active incoming parcels."""
        parcels = [
            p
            for p in (self.coordinator.data or {}).get("receiver", [])
            if not p.get("delivered")
        ]
        events: list[CalendarEvent] = []
        for parcel in parcels:
            start = _parse(parcel.get("planned_from"))
            if start is None:
                continue
            end = _parse(parcel.get("planned_to"))
            if end is None or end <= start:
                end = start + _DEFAULT_EVENT_DURATION

            barcode = parcel.get("barcode") or ""
            sender = parcel.get("sender")
            summary = sender or (f"Parcel {barcode}" if barcode else "PostNL parcel")
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=start,
                    end=end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming delivery event."""
        now = dt_util.now()
        upcoming = [event for event in self._events() if event.end > now]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all delivery events that overlap the requested range."""
        return [
            event
            for event in self._events()
            if event.start < end_date and event.end > start_date
        ]

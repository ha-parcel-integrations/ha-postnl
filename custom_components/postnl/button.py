"""Button platform for the PostNL integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PostNLConfigEntry
from .device import ATTRIBUTION, build_device_info

# A manual refresh is a single API round-trip; HA's per-entity throttling
# adds nothing here.
PARALLEL_UPDATES = 0




async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostNLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PostNL refresh button from a config entry."""
    async_add_entities([PostNLRefreshButton(entry)])


class PostNLRefreshButton(ButtonEntity):
    """Button that forces an immediate poll of PostNL.

    Useful when a parcel is expected and the user does not want to wait for
    the next scheduled refresh. Stateless from HA's perspective.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_attribution = ATTRIBUTION

    def __init__(self, entry: PostNLConfigEntry) -> None:
        """Initialise the refresh button."""
        self._entry = entry
        userinfo: dict[str, Any] = entry.runtime_data.userinfo
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_refresh"
        self._attr_device_info = build_device_info(userinfo)

    async def async_press(self) -> None:
        """Trigger an immediate refresh of the PostNL coordinator."""
        await self._entry.runtime_data.coordinator.async_request_refresh()

"""Image platform for the PostNL integration — photos of announced letters."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import PostNLConfigEntry
from .const import DOMAIN
from .coordinator import PostNLCoordinator
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)


def _letter_date_as_datetime(letter: dict | None) -> datetime | None:
    """Return the letter's parsed ISO date as a tz-aware datetime, or None."""
    if not letter:
        return None
    date_iso = letter.get("date")
    if not isinstance(date_iso, str) or not date_iso:
        return None
    try:
        return datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostNLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one image entity per MyMail letter and keep them in sync."""
    data = entry.runtime_data
    userinfo: dict[str, Any] = data.userinfo
    account_id: str = userinfo.get("account_id", "")
    coordinator = data.coordinator

    # coordinator.letters is already populated: __init__.py performs the first
    # refresh before platforms are forwarded, so ConfigEntryNotReady is raised
    # from the entry setup rather than (too late) from this forwarded platform.

    unique_prefix = f"{account_id}_letter_image_"
    known_ids: set[str] = set()

    def _current_ids() -> set[str]:
        return {
            letter["id"]
            for letter in (coordinator.letters or [])
            if letter.get("id") and letter.get("image_url")
        }

    initial_ids = _current_ids()
    if initial_ids:
        async_add_entities(
            PostNLLetterImage(hass, coordinator, entry, userinfo, letter_id)
            for letter_id in initial_ids
        )
        known_ids.update(initial_ids)

    @callback
    def _sync_letters() -> None:
        current_ids = _current_ids()

        new_ids = current_ids - known_ids
        if new_ids:
            async_add_entities(
                PostNLLetterImage(hass, coordinator, entry, userinfo, letter_id)
                for letter_id in new_ids
            )
            known_ids.update(new_ids)

        stale_ids = known_ids - current_ids
        if stale_ids:
            registry = er.async_get(hass)
            for letter_id in stale_ids:
                entity_id = registry.async_get_entity_id(
                    "image", DOMAIN, f"{unique_prefix}{letter_id}"
                )
                if entity_id:
                    registry.async_remove(entity_id)
            known_ids.difference_update(stale_ids)

    entry.async_on_unload(coordinator.async_add_listener(_sync_letters))


class PostNLLetterImage(CoordinatorEntity[PostNLCoordinator], ImageEntity):
    """Image entity exposing the scanned photo of a single MyMail letter."""

    _attr_has_entity_name = True
    _attr_translation_key = "letter_image"
    _attr_content_type = "image/jpeg"
    _attr_attribution = "Data provided by PostNL"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PostNLCoordinator,
        entry: ConfigEntry,
        userinfo: dict[str, Any],
        letter_id: str,
    ) -> None:
        """Initialize the image entity."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._entry = entry
        self._letter_id = letter_id
        account_id: str = userinfo.get("account_id", "")
        self._attr_unique_id = f"{account_id}_letter_image_{letter_id}"
        self._attr_device_info = build_device_info(userinfo)
        # Use the parsed letter date as the entity's "last updated" timestamp so
        # the state reflects when the letter was announced, not when HA booted.
        # Falls back to utcnow when the date couldn't be parsed.
        self._attr_image_last_updated = (
            _letter_date_as_datetime(self._letter()) or dt_util.utcnow()
        )
        self._image_url: str | None = (self._letter() or {}).get("image_url")
        self._cached_url: str | None = None
        self._cached_bytes: bytes | None = None
        self._apply_title()

    def _letter(self) -> dict | None:
        for letter in self.coordinator.letters or []:
            if letter.get("id") == self._letter_id:
                return letter
        return None

    def _apply_title(self) -> None:
        letter = self._letter()
        title = (letter or {}).get("title") or self._letter_id
        self._attr_translation_placeholders = {"title": title}

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self._letter() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        # Mirror the per-letter fields the sensor's ``letters`` attribute and
        # the ``postnl_letter_announced`` event already carry, so templates can
        # use whichever surface fits. ``image_url`` is intentionally omitted —
        # the image bytes are the entity's state.
        letter = self._letter() or {}
        return {
            "id": letter.get("id"),
            "title": letter.get("title"),
            "date": letter.get("date"),
            "unread": letter.get("unread"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        new_url = (self._letter() or {}).get("image_url")
        if new_url != self._image_url:
            # The letter photo changed; drop the cache and tell the frontend to refetch.
            self._image_url = new_url
            self._cached_url = None
            self._cached_bytes = None
            self._attr_image_last_updated = dt_util.utcnow()
        self._apply_title()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the current image bytes."""
        url = (self._letter() or {}).get("image_url")
        if not url:
            return None
        if self._cached_bytes is not None and self._cached_url == url:
            return self._cached_bytes

        try:
            # A client can request this photo at any point between polls, not
            # just right after one — the bearer token baked into jouw_api can
            # have expired since the last poll refreshed it (the default poll
            # interval is 30 minutes; PostNL's own access tokens do not
            # necessarily live that long). Ensure it is current before the
            # fetch instead of trusting whatever the coordinator last cached,
            # or this 401s. See CLAUDE.md — do not read
            # ``self.coordinator.jouw_api`` directly here.
            jouw_api = await self.coordinator.async_get_jouw_api()
            image_bytes, content_type = await self.hass.async_add_executor_job(
                jouw_api.image, url
            )
        except Exception as err:  # noqa: BLE001 - never let a bad image break the entity
            _LOGGER.warning(
                "Could not fetch PostNL letter image %s: %s", self._letter_id, err
            )
            return None

        self._attr_content_type = content_type
        self._cached_url = url
        self._cached_bytes = image_bytes
        return image_bytes

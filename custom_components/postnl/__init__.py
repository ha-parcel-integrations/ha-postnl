# SPDX-License-Identifier: Apache-2.0
# Derived from https://github.com/arjenbos/ha-postnl, Copyright (c) 2023 Arjen Bos; modified.
"""PostNL custom component for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
import requests
import urllib3

from .auth import AsyncConfigEntryAuth
from .const import DOMAIN, PLATFORMS
from .contents import ContentsStore
from .coordinator import PostNLCoordinator
from .login_api import PostNLLoginAPI
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class PostNLData:
    """Runtime data attached to a PostNL config entry."""

    auth: AsyncConfigEntryAuth
    coordinator: PostNLCoordinator
    userinfo: dict[str, Any]


type PostNLConfigEntry = ConfigEntry[PostNLData]


async def async_setup_entry(hass: HomeAssistant, entry: PostNLConfigEntry) -> bool:
    """Set up PostNL from config entry."""
    _LOGGER.debug("Setup Entry PostNL")

    auth = AsyncConfigEntryAuth(hass, entry)

    try:
        await auth.check_and_refresh_token()
    except ConfigEntryAuthFailed:
        # Credentials are genuinely invalid — let HA prompt for reauth.
        raise
    except HomeAssistantError as exception:
        # Transient auth/login failure — retry setup with backoff instead of
        # pushing the user into reauth.
        raise ConfigEntryNotReady("Unable to authenticate with PostNL") from exception

    postnl_login_api = PostNLLoginAPI(auth.access_token)

    try:
        userinfo = await hass.async_add_executor_job(postnl_login_api.userinfo)
    except (
        requests.exceptions.RequestException,
        urllib3.exceptions.MaxRetryError,
    ) as exception:
        raise ConfigEntryNotReady(
            "Unable to retrieve user information from PostNL."
        ) from exception

    if "error" in userinfo:
        raise ConfigEntryNotReady("Error in retrieving user information from PostNL.")

    coordinator = PostNLCoordinator(hass, entry)
    # runtime_data must be set before the first refresh: the coordinator reads
    # entry.runtime_data.auth inside _async_update_data.
    entry.runtime_data = PostNLData(
        auth=auth, coordinator=coordinator, userinfo=userinfo
    )

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff. It also means a single refresh instead of one per
    # platform (sensor + image previously each triggered their own).
    await coordinator.contents_store.async_load()
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PostNLConfigEntry) -> bool:
    """Unload PostNL config entry."""
    _LOGGER.debug("Unloading PostNL integration")
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared by every account, so they go with the last one.
    if not any(
        other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ):
        async_unload_services(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: PostNLConfigEntry) -> None:
    """Delete the contents the user set for the removed entry."""
    await ContentsStore(hass, entry.entry_id).async_remove()

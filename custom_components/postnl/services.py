"""Services for the PostNL integration.

`postnl.update_contents` sets or clears the text describing what is in a
parcel. The account lists its own parcels, so there is nothing to track by
hand — this is the only service. With several accounts it goes to the
account(s) that list the code, or to the one named by ``config_entry_id``.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import CONF_BARCODE, CONF_CONFIG_ENTRY_ID, CONF_CONTENTS, DOMAIN
from .contents import normalize_code

SERVICE_UPDATE_CONTENTS = "update_contents"

_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BARCODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the PostNL services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        return

    async def _update_contents(call: ServiceCall) -> None:
        barcode = normalize_code(call.data[CONF_BARCODE])
        if not barcode:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            candidate
            for candidate in hass.config_entries.async_entries(DOMAIN)
            if candidate.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("PostNL is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(
                    f"'{entry_id}' is not a loaded PostNL account"
                )
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two accounts, so every account that lists it
            # gets the value; guessing an account would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if barcode in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(
                barcode, call.data.get(CONF_CONTENTS)
            ):
                await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CONTENTS,
        _update_contents,
        schema=_UPDATE_CONTENTS_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the PostNL services once no account is loaded."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_remove(DOMAIN, SERVICE_UPDATE_CONTENTS)

"""Diagnostics support for the PostNL integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import PostNLConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "email",
    "account_id",
    "username",
    "barcode",
    "key",
    "name",
    "receiver",
    "receiver_title",
    "url",
    "phoneNumber",
    "postalCode",
    "street",
    "houseNumber",
    "city",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PostNLConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a PostNL config entry."""
    data = entry.runtime_data
    coordinator = data.coordinator
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "userinfo": async_redact_data(data.userinfo, TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "counts": {
            "receiver": len((coordinator.data or {}).get("receiver", [])),
            "sender": len((coordinator.data or {}).get("sender", [])),
            "delivered_receiver": len(coordinator.delivered_receiver or []),
            "delivered_sender": len(coordinator.delivered_sender or []),
            "letters": len(coordinator.letters or []),
        },
        "receiver": async_redact_data(
            (coordinator.data or {}).get("receiver", []), TO_REDACT
        ),
        "sender": async_redact_data(
            (coordinator.data or {}).get("sender", []), TO_REDACT
        ),
        "delivered_receiver": async_redact_data(
            coordinator.delivered_receiver or [], TO_REDACT
        ),
        "delivered_sender": async_redact_data(
            coordinator.delivered_sender or [], TO_REDACT
        ),
        "letters": async_redact_data(coordinator.letters or [], TO_REDACT),
    }

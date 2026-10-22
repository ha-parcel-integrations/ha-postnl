"""Tests for the PostNL refresh button."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.postnl.const import DOMAIN

_ENTRY_DATA = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "secret",
    "token": {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_at": 9_999_999_999,
    },
}
_USERINFO = {"account_id": "abc-123", "email": "user@example.com"}


def _add_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_ENTRY_DATA[CONF_USERNAME].lower(),
        data=_ENTRY_DATA,
        options={"delivered_filter_type": "days", "delivered_filter_amount": 7},
    )
    entry.add_to_hass(hass)
    return entry


def _mock_shipments() -> MagicMock:
    return MagicMock(
        return_value={
            "trackedShipments": {"receiverShipments": [], "senderShipments": []}
        }
    )


async def test_refresh_button_forces_a_poll(hass):
    """Pressing the refresh button re-polls the coordinator."""
    entry = _add_entry(hass)
    shipments = _mock_shipments()
    with (
        patch(
            "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "custom_components.postnl.PostNLLoginAPI.userinfo",
            new=MagicMock(return_value=_USERINFO),
        ),
        patch(
            "custom_components.postnl.coordinator.PostNLGraphql.shipments",
            new=shipments,
        ),
        patch(
            "custom_components.postnl.coordinator.PostNLJouwAPI.letters",
            new=MagicMock(return_value={"screen": {"sections": []}}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "button", DOMAIN, f"{_USERINFO['account_id']}_refresh"
        )
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None

        calls_before = shipments.call_count

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    assert shipments.call_count > calls_before

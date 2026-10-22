"""Tests for the PostNL device triggers."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components import automation
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.postnl.const import DOMAIN
from custom_components.postnl.device_trigger import TRIGGER_TYPES, async_get_triggers

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


async def _setup_and_get_device_id(hass):
    """Set up the integration and return the account's device id."""
    entry = _add_entry(hass)
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
            new=MagicMock(
                return_value={
                    "trackedShipments": {
                        "receiverShipments": [],
                        "senderShipments": [],
                    }
                }
            ),
        ),
        patch(
            "custom_components.postnl.coordinator.PostNLJouwAPI.letters",
            new=MagicMock(return_value={"screen": {"sections": []}}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, _USERINFO["account_id"])}
    )
    assert device is not None
    return device.id


async def test_get_triggers_lists_all_events(hass):
    """async_get_triggers returns one trigger per parcel/letter event."""
    device_id = await _setup_and_get_device_id(hass)

    triggers = await async_get_triggers(hass, device_id)

    assert {t["type"] for t in triggers} == TRIGGER_TYPES
    assert "letter_announced" in TRIGGER_TYPES
    assert {
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    } <= TRIGGER_TYPES
    assert all(t["domain"] == DOMAIN for t in triggers)
    assert all(t["device_id"] == device_id for t in triggers)


async def test_device_trigger_fires_automation(hass):
    """A device-trigger automation fires when the matching event is dispatched."""
    device_id = await _setup_and_get_device_id(hass)

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "trigger": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "type": "parcel_status_changed",
                },
                "action": {"event": "postnl_test_fired"},
            }
        },
    )
    await hass.async_block_till_done()

    fired: list = []
    hass.bus.async_listen("postnl_test_fired", lambda e: fired.append(e))

    hass.bus.async_fire(
        f"{DOMAIN}_parcel_status_changed",
        {"barcode": "A", "device_id": device_id},
    )
    await hass.async_block_till_done()
    assert len(fired) == 1

    hass.bus.async_fire(
        f"{DOMAIN}_parcel_status_changed",
        {"barcode": "B", "device_id": "some-other-device"},
    )
    await hass.async_block_till_done()
    assert len(fired) == 1

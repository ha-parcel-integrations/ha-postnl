"""Tests for the PostNL integration setup/unload entry points."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
import requests

from custom_components.postnl import PostNLData
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


async def test_setup_entry_succeeds_and_stores_runtime_data(hass):
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
            new=_mock_shipments(),
        ),
        patch(
            "custom_components.postnl.coordinator.PostNLJouwAPI.letters",
            new=MagicMock(return_value={"screen": {"sections": []}}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, PostNLData)
    assert entry.runtime_data.userinfo == _USERINFO


async def test_setup_entry_retries_on_userinfo_error(hass):
    """If the userinfo response contains 'error' we surface ConfigEntryNotReady."""
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "custom_components.postnl.PostNLLoginAPI.userinfo",
            new=MagicMock(return_value={"error": "rate-limited"}),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_reauths_on_invalid_credentials(hass):
    """A definitive auth failure during setup → reauth (SETUP_ERROR)."""
    entry = _add_entry(hass)
    with patch(
        "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token",
        new=AsyncMock(side_effect=ConfigEntryAuthFailed("invalid")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_entry_retries_on_transient_auth_failure(hass):
    """A transient auth/login failure during setup → retry, not reauth."""
    entry = _add_entry(hass)
    with patch(
        "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token",
        new=AsyncMock(side_effect=HomeAssistantError("re-login failed")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_retries_on_userinfo_network_failure(hass):
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "custom_components.postnl.PostNLLoginAPI.userinfo",
            new=MagicMock(side_effect=requests.exceptions.RequestException("boom")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_retries_when_first_refresh_fails(hass):
    """Auth and userinfo succeed but the first data fetch fails.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform (previously both sensor and
    image each triggered their own first refresh).
    """
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
            new=MagicMock(side_effect=requests.exceptions.RequestException("boom")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_entry_succeeds(hass):
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
            new=_mock_shipments(),
        ),
        patch(
            "custom_components.postnl.coordinator.PostNLJouwAPI.letters",
            new=MagicMock(return_value={"screen": {"sections": []}}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED

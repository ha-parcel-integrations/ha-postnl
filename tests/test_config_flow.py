"""Tests for the PostNL config flow."""
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.postnl.auth import PostNLAuthError
from custom_components.postnl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    DOMAIN,
)

_USER_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
_TOKEN = {
    "access_token": "tok",
    "refresh_token": "ref",
    "expires_at": 9_999_999_999,
}
_DELIVERED_INPUT = {
    CONF_DELIVERED_FILTER_TYPE: "days",
    CONF_DELIVERED_FILTER_AMOUNT: 14,
}


async def test_user_flow_creates_entry(hass):
    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(return_value=_TOKEN),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_INPUT
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "delivered"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DELIVERED_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == _USER_INPUT[CONF_USERNAME]
    assert result["data"]["token"] == _TOKEN
    assert result["options"][CONF_DELIVERED_FILTER_TYPE] == "days"
    assert result["options"][CONF_DELIVERED_FILTER_AMOUNT] == 14
    # Polling cadence is not an option — nothing is stored for it.
    assert "refresh_interval" not in result["options"]


async def test_user_flow_invalid_auth(hass):
    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(side_effect=PostNLAuthError("nope")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_when_already_configured(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
    ).add_to_hass(hass)

    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(return_value=_TOKEN),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_updates_credentials_and_reloads(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
    )
    entry.add_to_hass(hass)

    new_token = {**_TOKEN, "access_token": "new-tok"}
    with (
        patch(
            "custom_components.postnl.config_flow.PostNLAuth.async_login",
            new=AsyncMock(return_value=new_token),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            new=AsyncMock(return_value=True),
        ) as mock_reload,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: _USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "new"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
    assert entry.data["token"] == new_token
    mock_reload.assert_awaited_once_with(entry.entry_id)


async def test_reauth_flow_surfaces_invalid_auth(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(side_effect=PostNLAuthError("nope")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: _USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == _USER_INPUT[CONF_PASSWORD]


async def test_reauth_flow_aborts_on_different_account(hass):
    """Reauthenticating with another account's credentials aborts the flow."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.postnl.config_flow.PostNLAuth.async_login",
        new=AsyncMock(return_value=_TOKEN),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "other@example.com", CONF_PASSWORD: "pw"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_USERNAME] == _USER_INPUT[CONF_USERNAME]


async def test_options_flow_updates_filter_and_history(hass):
    """Submitting the sectioned options form persists both buckets."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
        options={CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "delivered": {
                CONF_DELIVERED_FILTER_TYPE: "parcels",
                CONF_DELIVERED_FILTER_AMOUNT: 21,
            },
            "history": {
                CONF_INCLUDE_HISTORY: True,
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 21
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    # The polling section is gone; nothing is written for it.
    assert "refresh_interval" not in result["data"]


async def test_options_flow_ignores_a_legacy_refresh_interval_option(hass):
    """An entry saved while the polling dropdown still existed keeps working.

    The stale ``refresh_interval`` value stays in the stored options until the
    user next saves the form, and is simply never read; submitting the form
    drops it without the flow tripping over it.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_USER_INPUT[CONF_USERNAME].lower(),
        data={**_USER_INPUT, "token": _TOKEN},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            "refresh_interval": 30,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "delivered": {
                CONF_DELIVERED_FILTER_TYPE: "days",
                CONF_DELIVERED_FILTER_AMOUNT: 7,
            },
            "history": {CONF_INCLUDE_HISTORY: False},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "refresh_interval" not in result["data"]

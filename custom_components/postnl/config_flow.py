"""Config flow for the postnl integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .auth import PostNLAuth, PostNLAuthError
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_NEW_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_AUTO,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})

_FILTER_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["days", "parcels"],
        translation_key=CONF_DELIVERED_FILTER_TYPE,
        mode=selector.SelectSelectorMode.LIST,
    )
)

_FILTER_AMOUNT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1,
        max=365,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
    )
)

_DELIVERED_SCHEMA = vol.Schema({
    vol.Required(
        CONF_DELIVERED_FILTER_TYPE, default=DEFAULT_DELIVERED_FILTER_TYPE
    ): _FILTER_TYPE_SELECTOR,
    vol.Required(
        CONF_DELIVERED_FILTER_AMOUNT, default=DEFAULT_DELIVERED_FILTER_AMOUNT
    ): _FILTER_AMOUNT_SELECTOR,
})


class PostNLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the postnl integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._username: str = ""
        self._password: str = ""
        self._token: dict | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PostNLOptionsFlowHandler:
        """Return the options flow handler."""
        return PostNLOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token, errors = await self._do_login(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                self._username = user_input[CONF_USERNAME]
                self._password = user_input[CONF_PASSWORD]
                self._token = token
                return await self.async_step_delivered()

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_delivered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the delivered-parcels filter form."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._username,
                data={
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    "token": self._token,
                },
                options={
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    # New installs default to dynamic polling; an entry set
                    # up before this option existed keeps reading
                    # DEFAULT_REFRESH_INTERVAL via the coordinator's .get()
                    # fallback instead (dynamic-polling.md Section 5.2).
                    CONF_REFRESH_INTERVAL: DEFAULT_NEW_REFRESH_INTERVAL,
                },
            )

        return self.async_show_form(
            step_id="delivered",
            data_schema=_DELIVERED_SCHEMA,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication on an auth error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the reauth confirmation step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token, errors = await self._do_login(user_input)
            if not errors:
                # Guard against re-authenticating with a *different* PostNL
                # account — the entry (and all its entities) belong to the
                # original account's unique_id.
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_mismatch()
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        "token": token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    async def _do_login(self, user_input: dict) -> tuple[dict | None, dict[str, str]]:
        try:
            token = await PostNLAuth(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            ).async_login()
            return token, {}
        except PostNLAuthError as err:
            _LOGGER.debug("PostNL login failed: %s", err)
            return None, {"base": "invalid_auth"}
        except aiohttp.ClientError as err:
            _LOGGER.debug("PostNL connection error: %s", err)
            return None, {"base": "cannot_connect"}


class PostNLOptionsFlowHandler(OptionsFlow):
    """Handle PostNL options — delivered-parcels filter plus polling cadence.

    The form is rendered with two collapsible sections (``delivered`` and
    ``polling``) so the unrelated knobs don't compete for attention. HA
    returns the user input nested by section name; we flatten it before
    storing on the config entry so the coordinator can keep reading the
    flat keys directly.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            delivered = user_input.get("delivered", {})
            history = user_input.get("history", {})
            polling = user_input.get("polling", {})
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    CONF_REFRESH_INTERVAL: (
                        REFRESH_INTERVAL_AUTO
                        if polling[CONF_REFRESH_INTERVAL] == REFRESH_INTERVAL_AUTO
                        else int(polling[CONF_REFRESH_INTERVAL])
                    ),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("delivered"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    default=current.get(
                                        CONF_DELIVERED_FILTER_TYPE,
                                        DEFAULT_DELIVERED_FILTER_TYPE,
                                    ),
                                ): _FILTER_TYPE_SELECTOR,
                                vol.Required(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    default=current.get(
                                        CONF_DELIVERED_FILTER_AMOUNT,
                                        DEFAULT_DELIVERED_FILTER_AMOUNT,
                                    ),
                                ): _FILTER_AMOUNT_SELECTOR,
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required("history"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_INCLUDE_HISTORY,
                                    default=current.get(
                                        CONF_INCLUDE_HISTORY,
                                        DEFAULT_INCLUDE_HISTORY,
                                    ),
                                ): selector.BooleanSelector(),
                            }
                        ),
                        {"collapsed": True},
                    ),
                    vol.Required("polling"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_REFRESH_INTERVAL,
                                    # str(): the selector's option values are
                                    # strings, so the default must be a string
                                    # too — a stored int won't match and trips
                                    # "expected str" validation on submit.
                                    default=str(current.get(
                                        CONF_REFRESH_INTERVAL,
                                        DEFAULT_REFRESH_INTERVAL,
                                    )),
                                ): selector.SelectSelector(
                                    selector.SelectSelectorConfig(
                                        options=[REFRESH_INTERVAL_AUTO]
                                        + [str(m) for m in REFRESH_INTERVAL_OPTIONS],
                                        translation_key=CONF_REFRESH_INTERVAL,
                                        mode=selector.SelectSelectorMode.DROPDOWN,
                                    )
                                ),
                            }
                        ),
                        {"collapsed": True},
                    ),
                }
            ),
        )

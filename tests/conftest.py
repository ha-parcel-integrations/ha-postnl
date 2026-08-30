"""pytest configuration for the PostNL test suite."""
import sys

import pytest
from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.postnl`` loadable in HA tests."""
    yield


@pytest.fixture(autouse=True)
def reset_one_shot_warnings():
    """Clear the "already warned this session" flags between tests.

    They are module-level by design -- a user must not be told about the
    same unmapped status or unconfirmed field on every poll -- but that also
    makes them leak across tests, so whether a warning fires would otherwise
    depend on test order.
    """
    from custom_components.postnl import parcels

    parcels._LOGGED_UNKNOWN_STATUSES.clear()
    parcels._LOGGED_UNKNOWN_OBSERVATION_CODES.clear()
    yield


if sys.platform == "win32":
    # pytest-homeassistant-custom-component blocks socket *creation*
    # (``disable_socket(allow_unix_socket=True)``) in its per-test setup hook.
    # That is fine on Linux, where asyncio's self-pipe is an AF_UNIX
    # socketpair — but Windows event loops build theirs from AF_INET sockets,
    # so every async test dies with ``SocketBlockedError`` while the event
    # loop fixture is being created. Neutralise the creation block on Windows
    # and keep the network guard as the plugin's connect-time allowlist
    # (``socket_allow_hosts(["127.0.0.1"])``, applied right before the
    # ``disable_socket`` call we swallow here).
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None

    # HA's aiohttp helper hardcodes aiohttp's AsyncResolver, whose aiodns
    # backend refuses the Proactor loop the suite runs on under Windows.
    # Swap in the threaded resolver for the tests — no test resolves DNS.
    import homeassistant.helpers.aiohttp_client as _ha_aiohttp_client
    from aiohttp.resolver import ThreadedResolver

    _ha_aiohttp_client.AsyncResolver = ThreadedResolver

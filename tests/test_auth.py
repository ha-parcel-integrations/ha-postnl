"""Tests for the PostNL auth module.

Covers the orchestration helper ``AsyncConfigEntryAuth`` plus the
small static helpers on ``PostNLAuth``. The long PKCE + Capture login
flow itself is covered by an end-to-end test that drives a mocked
aiohttp session through every step.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
import pytest

from custom_components.postnl.auth import (
    AsyncConfigEntryAuth,
    PostNLAuth,
    PostNLAuthError,
    PostNLInvalidAuth,
)

# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_pkce_pair_challenge_matches_verifier():
    verifier, challenge = PostNLAuth._pkce_pair()
    assert verifier and challenge
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected


def test_pkce_pair_returns_new_values_each_time():
    a = PostNLAuth._pkce_pair()
    b = PostNLAuth._pkce_pair()
    assert a != b


def test_csrf_from_jar_returns_value_when_cookie_present():
    cookie = MagicMock()
    cookie.key = "_csrf_token"
    cookie.value = "abc123"
    jar = [cookie]
    assert PostNLAuth._csrf_from_jar(jar) == "abc123"


def test_csrf_from_jar_returns_none_when_missing():
    cookie = MagicMock()
    cookie.key = "other"
    cookie.value = "v"
    assert PostNLAuth._csrf_from_jar([cookie]) is None


def test_js_value_extracts_value_from_widget_body():
    body = "var foo = { aicCsrf: 'xyz', other: 'no' };"
    assert PostNLAuth._js_value(body, "aicCsrf:") == "xyz"


def test_js_value_returns_none_when_missing():
    assert PostNLAuth._js_value("nothing", "key:") is None


def test_json_value_extracts_quoted_pair():
    body = '{"accessToken":"capturetok","other":"x"}'
    assert PostNLAuth._json_value(body, "accessToken") == "capturetok"


def test_json_value_returns_none_when_missing():
    assert PostNLAuth._json_value("{}", "accessToken") is None


# ---------------------------------------------------------------------------
# _token_request / async_refresh_token
# ---------------------------------------------------------------------------


def _aiohttp_session(*responses):
    """Build a fake aiohttp session whose `.get`/`.post` yield responses in order."""
    queue = list(responses)

    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield queue.pop(0)

    session = MagicMock()
    session.get = MagicMock(side_effect=_ctx)
    session.post = MagicMock(side_effect=_ctx)
    return session


def _response(*, json_data=None, text="", headers=None, status=200):
    resp = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=b"")
    resp.headers = headers or {}
    resp.status = status
    resp.url = "https://login.postnl.nl/login/authorize?client_id=test"
    return resp


@pytest.mark.asyncio
async def test_token_request_returns_normalised_dict():
    session = _aiohttp_session(
        _response(
            json_data={
                "access_token": "newat",
                "refresh_token": "newrt",
                "expires_in": 1800,
                "token_type": "Bearer",
            }
        )
    )
    before = time.time()
    token = await PostNLAuth._token_request(session, {"grant_type": "refresh_token"})
    after = time.time()

    assert token["access_token"] == "newat"
    assert token["refresh_token"] == "newrt"
    assert token["expires_in"] == 1800
    assert before + 1800 <= token["expires_at"] <= after + 1800
    assert token["token_type"] == "Bearer"


@pytest.mark.asyncio
async def test_token_request_raises_with_error_description():
    session = _aiohttp_session(
        _response(
            json_data={
                "error": "invalid_grant",
                "error_description": "Refresh token expired",
            }
        )
    )
    with pytest.raises(PostNLAuthError, match="Refresh token expired"):
        await PostNLAuth._token_request(session, {})


@pytest.mark.asyncio
async def test_token_request_raises_with_error_when_no_description():
    session = _aiohttp_session(_response(json_data={"error": "invalid_grant"}))
    with pytest.raises(PostNLAuthError, match="invalid_grant"):
        await PostNLAuth._token_request(session, {})


@pytest.mark.asyncio
async def test_token_request_raises_with_unknown_when_no_error_info():
    session = _aiohttp_session(_response(json_data={}))
    with pytest.raises(PostNLAuthError, match="unknown"):
        await PostNLAuth._token_request(session, {})


@pytest.mark.asyncio
async def test_token_request_defaults_expires_in_when_missing():
    session = _aiohttp_session(_response(json_data={"access_token": "x"}))
    token = await PostNLAuth._token_request(session, {})
    assert token["expires_in"] == 3600
    assert token["refresh_token"] is None


@pytest.mark.asyncio
async def test_async_refresh_token_delegates_to_token_request():
    with patch(
        "custom_components.postnl.auth.PostNLAuth._token_request",
        new=AsyncMock(return_value={"access_token": "from_refresh"}),
    ) as mock_call:
        result = await PostNLAuth.async_refresh_token("oldrt")

    assert result == {"access_token": "from_refresh"}
    # The data dict passed in must include grant_type=refresh_token + the rt.
    payload = mock_call.call_args[0][1]
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "oldrt"


# ---------------------------------------------------------------------------
# AsyncConfigEntryAuth
# ---------------------------------------------------------------------------


def _entry(
    token: dict | None,
    *,
    username: str | None = "user@example.com",
    password: str | None = "secret",  # noqa: S107 - test value
) -> MagicMock:
    entry = MagicMock()
    data: dict = {"token": token} if token is not None else {}
    if username:
        data["username"] = username
    if password:
        data["password"] = password
    entry.data = data
    return entry


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


@pytest.mark.asyncio
async def test_check_and_refresh_token_raises_when_no_token():
    auth = AsyncConfigEntryAuth(_hass(), _entry(None))
    with pytest.raises(ConfigEntryAuthFailed):
        await auth.check_and_refresh_token()


@pytest.mark.asyncio
async def test_check_and_refresh_token_raises_when_token_lacks_access_token():
    auth = AsyncConfigEntryAuth(_hass(), _entry({"refresh_token": "x"}))
    with pytest.raises(ConfigEntryAuthFailed):
        await auth.check_and_refresh_token()


@pytest.mark.asyncio
async def test_check_and_refresh_token_returns_current_when_still_valid():
    far_future = time.time() + 3600
    entry = _entry(
        {"access_token": "current", "expires_at": far_future, "refresh_token": "rt"}
    )
    auth = AsyncConfigEntryAuth(_hass(), entry)
    assert await auth.check_and_refresh_token() == "current"


@pytest.mark.asyncio
async def test_check_and_refresh_token_uses_refresh_when_expired():
    new_token = {
        "access_token": "rotated",
        "refresh_token": "newrt",
        "expires_at": time.time() + 1800,
    }
    entry = _entry(
        {"access_token": "old", "expires_at": time.time() - 60, "refresh_token": "rt"}
    )
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with patch(
        "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
        new=AsyncMock(return_value=new_token),
    ) as mock_refresh:
        result = await auth.check_and_refresh_token()

    assert result == "rotated"
    mock_refresh.assert_awaited_once_with("rt")
    hass.config_entries.async_update_entry.assert_called_once()
    updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data["token"] == new_token


@pytest.mark.asyncio
async def test_check_and_refresh_token_falls_back_to_login_when_refresh_fails():
    new_token = {"access_token": "fresh"}
    entry = _entry({"access_token": "old", "expires_at": 0, "refresh_token": "rt"})
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with (
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
            new=AsyncMock(side_effect=PostNLAuthError("expired")),
        ),
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_login",
            new=AsyncMock(return_value=new_token),
        ),
    ):
        result = await auth.check_and_refresh_token()

    assert result == "fresh"


@pytest.mark.asyncio
async def test_check_and_refresh_token_skips_refresh_when_no_refresh_token_present():
    new_token = {"access_token": "fresh"}
    entry = _entry({"access_token": "old", "expires_at": 0})
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with patch(
        "custom_components.postnl.auth.PostNLAuth.async_login",
        new=AsyncMock(return_value=new_token),
    ) as mock_login:
        result = await auth.check_and_refresh_token()

    assert result == "fresh"
    mock_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_and_refresh_token_reauths_when_credentials_invalid():
    """A definitive credential rejection during re-login → reauth."""
    entry = _entry({"access_token": "old", "expires_at": 0, "refresh_token": "rt"})
    auth = AsyncConfigEntryAuth(_hass(), entry)

    with (
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
            new=AsyncMock(side_effect=PostNLAuthError("expired")),
        ),
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_login",
            new=AsyncMock(side_effect=PostNLInvalidAuth("check your credentials")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await auth.check_and_refresh_token()


@pytest.mark.asyncio
async def test_check_and_refresh_token_retries_when_login_transiently_fails():
    """A transient re-login hiccup must NOT log the user out — it should
    surface as a generic (retryable) error, not ConfigEntryAuthFailed."""
    entry = _entry({"access_token": "old", "expires_at": 0, "refresh_token": "rt"})
    auth = AsyncConfigEntryAuth(_hass(), entry)

    with (
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
            new=AsyncMock(side_effect=PostNLAuthError("expired")),
        ),
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_login",
            new=AsyncMock(side_effect=PostNLAuthError("recaptcha challenge")),
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await auth.check_and_refresh_token()

    assert not isinstance(excinfo.value, ConfigEntryAuthFailed)


@pytest.mark.asyncio
async def test_check_and_refresh_token_preserves_refresh_token_when_response_omits_it():
    """PostNL sometimes refreshes without returning a new refresh token; the
    existing one must be kept so the next cycle can still refresh."""
    new_token = {
        "access_token": "rotated",
        "refresh_token": None,
        "expires_at": time.time() + 1800,
    }
    entry = _entry({"access_token": "old", "expires_at": 0, "refresh_token": "keepme"})
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with patch(
        "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
        new=AsyncMock(return_value=new_token),
    ):
        result = await auth.check_and_refresh_token()

    assert result == "rotated"
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated["token"]["refresh_token"] == "keepme"


@pytest.mark.asyncio
async def test_check_and_refresh_token_returns_early_when_another_caller_refreshed():
    """If a concurrent caller refreshes while we wait for the lock, the
    re-check inside the lock returns the fresh token without refreshing again."""
    expired = {"access_token": "old", "expires_at": 0, "refresh_token": "rt"}
    refreshed = {
        "access_token": "by-other",
        "expires_at": time.time() + 3600,
        "refresh_token": "rt",
    }
    entry = MagicMock()
    # First read (outer check) sees the expired token; the read inside the lock
    # sees the token a concurrent caller already refreshed.
    entry.data = MagicMock()
    entry.data.get = MagicMock(side_effect=[expired, refreshed])
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with patch(
        "custom_components.postnl.auth.PostNLAuth.async_refresh_token", new=AsyncMock()
    ) as mock_refresh:
        result = await auth.check_and_refresh_token()

    assert result == "by-other"
    mock_refresh.assert_not_awaited()
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_login_raises_invalid_auth_when_capture_returns_no_token():
    """Bad credentials → Capture yields no access token → PostNLInvalidAuth."""
    login_url = "https://login.postnl.nl/login/authorize?client_id=x&state=s"
    authorize_resp = _response(text="aicCsrf: 'csrf123'")
    authorize_resp.url = login_url
    capture_submit_resp = _response()
    capture_poll_resp = _response(text="{}")  # no accessToken in the result

    session = _aiohttp_session(authorize_resp, capture_submit_resp, capture_poll_resp)
    session.cookie_jar = []

    with (
        patch(
            "custom_components.postnl.auth.aiohttp.ClientSession",
            return_value=_AsyncContext(session),
        ),
        patch.object(PostNLAuth, "_pkce_pair", return_value=("v", "c")),
        pytest.raises(PostNLInvalidAuth),
    ):
        await PostNLAuth("user", "pw").async_login()


@pytest.mark.asyncio
async def test_check_and_refresh_token_raises_when_no_creds_and_refresh_fails():
    entry = _entry(
        {"access_token": "old", "expires_at": 0, "refresh_token": "rt"},
        username=None,
        password=None,
    )
    hass = _hass()
    auth = AsyncConfigEntryAuth(hass, entry)

    with (
        patch(
            "custom_components.postnl.auth.PostNLAuth.async_refresh_token",
            new=AsyncMock(side_effect=PostNLAuthError("expired")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await auth.check_and_refresh_token()


def test_access_token_property_reads_from_entry():
    entry = _entry({"access_token": "abc"})
    auth = AsyncConfigEntryAuth(_hass(), entry)
    assert auth.access_token == "abc"


# ---------------------------------------------------------------------------
# async_login — end-to-end mocked PKCE + Capture flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_login_walks_full_pkce_capture_flow():
    """Drive a full successful login through a mocked aiohttp session.

    Each of the six steps in PostNLAuth._login is satisfied by a
    pre-canned response: authorize → capture submit → capture poll →
    token-url (returns redirect) → authorize redirect → token exchange.
    """
    login_url = "https://login.postnl.nl/login/authorize?client_id=x&state=s"
    auth_url = "https://login.postnl.nl/auth?ok=1"
    final_redirect = "https://www.postnl.nl/?code=AUTHCODE&state=STATE_VALUE"

    authorize_resp = _response(text="aicCsrf: 'csrf123'")
    authorize_resp.url = login_url
    capture_submit_resp = _response()
    capture_poll_resp = _response(text='{"accessToken":"capturetok"}')
    token_url_resp = _response(headers={"Location": auth_url})
    authorize_redirect_resp = _response(headers={"Location": final_redirect})
    token_resp = _response(
        json_data={
            "access_token": "finaltoken",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
    )

    session = _aiohttp_session(
        authorize_resp,  # step 1: GET authorize
        capture_submit_resp,  # step 2: POST capture
        capture_poll_resp,  # step 3: GET capture result
        token_url_resp,  # step 4: POST token-url
        authorize_redirect_resp,  # step 5: GET auth_url
        token_resp,  # step 6: POST token
    )
    session.cookie_jar = []  # _csrf_from_jar fallback path

    with (
        patch(
            "custom_components.postnl.auth.aiohttp.ClientSession",
            return_value=_AsyncContext(session),
        ),
        patch(
            "custom_components.postnl.auth.secrets.token_bytes",
            return_value=b"\x00" * 24,
        ),
        patch.object(PostNLAuth, "_pkce_pair", return_value=("verifier", "challenge")),
        # Patch out the state check so the canned final_redirect's
        # static "STATE_VALUE" matches whatever we generated.
        patch(
            "custom_components.postnl.auth.base64.urlsafe_b64encode",
            return_value=b"STATE_VALUE",
        ),
    ):
        token = await PostNLAuth("user", "pw").async_login()

    assert token["access_token"] == "finaltoken"
    assert token["refresh_token"] == "rt"


class _AsyncContext:
    """Async context manager that yields the wrapped session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False

"""OAuth2 authentication for the postnl integration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, urlparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

_LOGGER = logging.getLogger(__name__)

_BASE = "https://login.postnl.nl"
_TENANT = "101112a0-4a0f-4bbb-8176-2f1b2d370d7c"
_OIDC_CLIENT_ID = "bd9f1610-b56d-4e05-a09b-f696f05ddade"
_CAPTURE_CLIENT_ID = "dkyxkt9x888ye422mawmf769yfm9y44j"
_REDIRECT_URI = "https://www.postnl.nl/"
_SCOPE = "openid poa-profiles-api offline_access"
_FLOW_VERSION = "20250910094830574377"
# suite: allow browser user-agent (the login flow is the website's own)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


class PostNLAuthError(Exception):
    """Raised on an API error."""

    pass


class PostNLInvalidAuth(PostNLAuthError):
    """Raised when PostNL rejects the username/password outright.

    Distinct from a generic ``PostNLAuthError`` so callers can tell a
    definitive "your credentials are wrong" (reauth) apart from a
    transient login hiccup (retry).
    """


class PostNLAuth:
    """OAuth2 session helper bound to a config entry."""

    def __init__(self, username: str, password: str) -> None:
        """Initialize the auth helper."""
        self._username = username
        self._password = password

    async def async_login(self) -> dict:
        """Run the full PKCE + Capture login flow and return a token dict."""
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            return await self._login(session)

    async def _login(self, session: aiohttp.ClientSession) -> dict:
        verifier, challenge = self._pkce_pair()
        state = base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode()

        # Step 1: open the OIDC authorize endpoint to establish cookies
        async with session.get(
            f"{_BASE}/{_TENANT}/login/authorize",
            params={
                "client_id": _OIDC_CLIENT_ID,
                "response_type": "code",
                "scope": _SCOPE,
                "redirect_uri": _REDIRECT_URI,
                "state": state,
                "nonce": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        ) as resp:
            login_url = str(resp.url)
            body = await resp.text()

        csrf = self._csrf_from_jar(session.cookie_jar) or self._js_value(body, "aicCsrf:")
        if not csrf:
            raise PostNLAuthError("Could not find CSRF token")

        # Step 2: submit credentials to the Capture widget
        txid = base64.urlsafe_b64encode(secrets.token_bytes(30)).rstrip(b"=").decode()
        async with session.post(
            f"{_BASE}/widget/traditional_signin.jsonp",
            data={
                "utf8": "✓",
                "signInEmailAddress": self._username,
                "currentPassword": self._password,
                "capture_screen": "signIn",
                "js_version": "d445bf4",
                "capture_transactionId": txid,
                "form": "signInForm",
                "flow": "standard",
                "client_id": _CAPTURE_CLIENT_ID,
                "redirect_uri": f"{login_url}&socialRedirect=True",
                "response_type": "token",
                "flow_version": _FLOW_VERSION,
                "settings_version": "",
                "locale": "en-US",
                "recaptchaVersion": "2",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": _BASE,
                "Referer": login_url,
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            await resp.read()

        # Step 3: poll for Capture result
        async with session.get(
            f"{_BASE}/widget/get_result.jsonp",
            params={
                "transactionId": txid,
                "cache": str(int(time.time() * 1000)),
            },
            headers={"User-Agent": _USER_AGENT},
        ) as resp:
            result_body = await resp.text()

        capture_token = self._json_value(result_body, "accessToken")
        if not capture_token:
            raise PostNLInvalidAuth(
                "Capture did not return an access token — check your credentials"
            )

        # Step 4: exchange the Capture token for an OIDC auth code
        qs = urlparse(login_url).query
        token_url = f"{_BASE}/{_TENANT}/auth-ui/v2/token-url?{qs}"

        auth_url, body = await self._post_token_url(
            session,
            token_url=token_url,
            referer=login_url,
            data={
                "screen": "signIn",
                "authenticated": "True",
                "registering": "False",
                "accessToken": capture_token,
                "_csrf_token": csrf,
            },
        )

        if not auth_url:
            # Server wants a second POST acknowledging the loginSuccess screen
            existing_token = self._js_value(body, "existingToken:")
            screen = self._js_value(body, "screenToRender:")
            csrf = self._js_value(body, "aicCsrf:")
            if screen != "loginSuccess" or not existing_token or not csrf:
                raise PostNLAuthError("Hosted Login did not reach loginSuccess")

            auth_url, _ = await self._post_token_url(
                session,
                token_url=token_url,
                referer=token_url,
                data={
                    "screen": "loginSuccess",
                    "accessToken": existing_token,
                    "_csrf_token": csrf,
                },
            )

        if not auth_url:
            raise PostNLAuthError("Hosted Login did not return an authorize redirect")

        # Step 5: follow the authorize redirect to capture the auth code
        async with session.get(
            auth_url,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=False,
        ) as resp:
            final_location = resp.headers.get("Location", "")

        qs_params = parse_qs(urlparse(final_location).query)
        code = qs_params.get("code", [None])[0]
        returned_state = qs_params.get("state", [None])[0]

        if not code:
            raise PostNLAuthError("OIDC authorize did not return a code")
        if returned_state != state:
            raise PostNLAuthError("OIDC state mismatch")

        # Step 6: exchange code for tokens
        return await self._token_request(session, {
            "grant_type": "authorization_code",
            "client_id": _OIDC_CLIENT_ID,
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        })

    @staticmethod
    async def async_refresh_token(refresh_token: str) -> dict:
        """Exchange a refresh token for a new token dict."""
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            return await PostNLAuth._token_request(session, {
                "grant_type": "refresh_token",
                "client_id": _OIDC_CLIENT_ID,
                "refresh_token": refresh_token,
            })

    @staticmethod
    async def _post_token_url(
        session: aiohttp.ClientSession,
        token_url: str,
        referer: str,
        data: dict,
    ) -> tuple[str | None, str]:
        async with session.post(
            token_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": _BASE,
                "Referer": referer,
                "User-Agent": _USER_AGENT,
            },
            allow_redirects=False,
        ) as resp:
            body = await resp.text()
            return resp.headers.get("Location"), body

    @staticmethod
    async def _token_request(session: aiohttp.ClientSession, data: dict) -> dict:
        async with session.post(
            f"{_BASE}/{_TENANT}/login/token",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            token_data = await resp.json(content_type=None)

        if "access_token" not in token_data:
            error = token_data.get("error_description") or token_data.get("error", "unknown")
            raise PostNLAuthError(f"Token request failed: {error}")

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in", 3600),
            "expires_at": time.time() + token_data.get("expires_in", 3600),
            "token_type": token_data.get("token_type", "Bearer"),
        }

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    @staticmethod
    def _csrf_from_jar(jar: aiohttp.CookieJar) -> str | None:
        for cookie in jar:
            if cookie.key == "_csrf_token":
                return cookie.value
        return None

    @staticmethod
    def _js_value(body: str, key: str) -> str | None:
        m = re.search(rf"{re.escape(key)}\s*['\"]([^'\"]+)['\"]", body)
        return m.group(1) if m else None

    @staticmethod
    def _json_value(body: str, key: str) -> str | None:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', body)
        return m.group(1) if m else None


class AsyncConfigEntryAuth:
    """Manage PostNL tokens stored in a config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the auth helper."""
        self._hass = hass
        self._entry = entry
        self._lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        """Return the current access token."""
        return self._entry.data["token"]["access_token"]

    async def check_and_refresh_token(self) -> str:
        """Refresh the access token when it has expired."""
        token = self._entry.data.get("token")

        if not token or "access_token" not in token:
            raise ConfigEntryAuthFailed("No valid token in config entry")

        if time.time() < token.get("expires_at", 0) - 30:
            return token["access_token"]

        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            # Re-read and bail out early if the token is valid again — this also
            # stops two callers from spending the same rotating refresh token
            # twice, which PostNL rejects and which would force a re-login.
            token = self._entry.data.get("token") or {}
            if "access_token" in token and time.time() < token.get("expires_at", 0) - 30:
                return token["access_token"]

            _LOGGER.debug("Access token expired, refreshing")
            refresh_token = token.get("refresh_token")
            if refresh_token:
                try:
                    new_token = await PostNLAuth.async_refresh_token(refresh_token)
                    # PostNL does not always return a fresh refresh token on a
                    # refresh. Keep the existing one so the next cycle can still
                    # refresh instead of being forced into a full re-login.
                    if not new_token.get("refresh_token"):
                        new_token["refresh_token"] = refresh_token
                    self._hass.config_entries.async_update_entry(
                        self._entry,
                        data={**self._entry.data, "token": new_token},
                    )
                    return new_token["access_token"]
                except PostNLAuthError as err:
                    _LOGGER.debug("Token refresh failed, falling back to re-login: %s", err)

            username = self._entry.data.get("username")
            password = self._entry.data.get("password")
            if username and password:
                try:
                    new_token = await PostNLAuth(username, password).async_login()
                    self._hass.config_entries.async_update_entry(
                        self._entry,
                        data={**self._entry.data, "token": new_token},
                    )
                    return new_token["access_token"]
                except PostNLInvalidAuth as err:
                    # Credentials are genuinely wrong — prompt the user to reauth.
                    _LOGGER.debug("Re-login rejected credentials, triggering reauth: %s", err)
                    raise ConfigEntryAuthFailed("Invalid PostNL credentials") from err
                except PostNLAuthError as err:
                    # Transient login hiccup (recaptcha, rate limit, a changed
                    # login widget, a network blip mid-flow). Do NOT drop the
                    # session — raise a generic error so the coordinator turns it
                    # into a retryable UpdateFailed and the next poll recovers.
                    _LOGGER.warning("PostNL re-login failed, will retry: %s", err)
                    raise HomeAssistantError("PostNL re-login failed") from err

            raise ConfigEntryAuthFailed("Unable to obtain a valid token")

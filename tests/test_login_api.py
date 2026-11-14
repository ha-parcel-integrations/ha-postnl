"""Unit tests for the login.postnl.nl API client."""

from unittest.mock import MagicMock

from custom_components.postnl.const import USER_AGENT
from custom_components.postnl.login_api import PostNLLoginAPI


def _client_with_response(json_data=None):
    client = PostNLLoginAPI(access_token="tok")
    response = MagicMock()
    response.json.return_value = json_data or {}
    client.client.get = MagicMock(return_value=response)
    return client, response


def test_userinfo_returns_parsed_json():
    client, _ = _client_with_response(json_data={"account_id": "abc"})
    assert client.userinfo() == {"account_id": "abc"}
    assert "profiles/oidc/userinfo" in client.client.get.call_args[0][0]


def test_userinfo_sends_a_timeout():
    """Without one a silent login server blocks setup forever (#50)."""
    client, _ = _client_with_response()
    client.userinfo()
    assert client.client.get.call_args.kwargs["timeout"] == (10, 30)


def test_session_sends_the_browser_user_agent():
    """A default python-requests agent is what the login host stalls on (#50)."""
    client, _ = _client_with_response()
    assert client.client.headers["User-Agent"] == USER_AGENT
    assert client.client.headers["Accept"] == "application/json"
    assert client.client.headers["Authorization"] == "Bearer tok"

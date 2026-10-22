"""Unit tests for the jouw.postnl.nl API client."""

from unittest.mock import MagicMock

from custom_components.postnl.jouw_api import PostNLJouwAPI


def _client_with_response(json_data=None, *, content=b"", content_type="image/jpeg"):
    client = PostNLJouwAPI(access_token="tok")
    response = MagicMock()
    response.json.return_value = json_data or {}
    response.content = content
    response.headers = {"content-type": content_type}
    response.raise_for_status = MagicMock()
    client.client.get = MagicMock(return_value=response)
    return client, response


def test_track_and_trace_passes_key_and_language():
    client, _ = _client_with_response(json_data={"colli": {}})
    result = client.track_and_trace("3SABC-NL-1234AB")
    assert result == {"colli": {}}
    call_url = client.client.get.call_args[0][0]
    assert "3SABC-NL-1234AB" in call_url
    assert "language=nl" in call_url


def test_letters_sends_mymail_headers():
    client, response = _client_with_response(json_data={"screen": {}})
    result = client.letters()
    assert result == {"screen": {}}
    response.raise_for_status.assert_called_once()
    # The call should include the MyMail app-identification headers
    headers = client.client.get.call_args.kwargs["headers"]
    for key in ("api-version", "app-platform", "device-token"):
        assert key in headers


def test_image_returns_bytes_and_content_type():
    client, _ = _client_with_response(content=b"PNGDATA", content_type="image/png")
    image_bytes, content_type = client.image("https://example.com/img")
    assert image_bytes == b"PNGDATA"
    assert content_type == "image/png"


def test_image_defaults_content_type_when_header_missing():
    client = PostNLJouwAPI(access_token="tok")
    response = MagicMock()
    response.content = b"bytes"
    response.headers = {}
    response.raise_for_status = MagicMock()
    client.client.get = MagicMock(return_value=response)

    _, content_type = client.image("https://example.com/img")
    assert content_type == "image/jpeg"

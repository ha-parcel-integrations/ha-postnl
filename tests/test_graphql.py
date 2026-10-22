"""Tests for the GraphQL wrapper.

The transport is mocked so we never hit the network. We just verify
that profile() / shipments() build a GraphQL document and delegate to
the client's `execute` method, and that call() is the same one-liner.
"""

from unittest.mock import MagicMock, patch

from custom_components.postnl.graphql import PostNLGraphql


def test_init_configures_transport_with_bearer_token():
    with (
        patch(
            "custom_components.postnl.graphql.RequestsHTTPTransport"
        ) as transport_cls,
        patch("custom_components.postnl.graphql.Client") as client_cls,
    ):
        PostNLGraphql("mytok")

    transport_cls.assert_called_once()
    headers = transport_cls.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer mytok"
    client_cls.assert_called_once()


def test_call_executes_query_via_client():
    api = PostNLGraphql("tok")
    api.client = MagicMock()
    api.client.execute.return_value = {"data": {"ok": True}}

    result = api.call("query { ping }")
    assert result == {"data": {"ok": True}}
    api.client.execute.assert_called_once()


def test_profile_executes_graphql_query():
    api = PostNLGraphql("tok")
    api.client = MagicMock()
    api.client.execute.return_value = {"profile": {"username": "u"}}

    result = api.profile()
    assert result == {"profile": {"username": "u"}}
    # The DSL document passed to execute is built from the inline query string.
    api.client.execute.assert_called_once()


def test_shipments_executes_graphql_query():
    api = PostNLGraphql("tok")
    api.client = MagicMock()
    api.client.execute.return_value = {
        "trackedShipments": {"receiverShipments": [], "senderShipments": []},
    }

    result = api.shipments()
    assert result["trackedShipments"]["receiverShipments"] == []
    api.client.execute.assert_called_once()

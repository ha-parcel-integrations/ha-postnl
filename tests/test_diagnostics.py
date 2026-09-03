"""Tests for the PostNL diagnostics handler."""
from unittest.mock import MagicMock

from custom_components.postnl import PostNLData
from custom_components.postnl.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)

REDACTED = "**REDACTED**"


def _entry(
    *,
    receiver: list[dict] | None = None,
    sender: list[dict] | None = None,
    delivered_receiver: list[dict] | None = None,
    delivered_sender: list[dict] | None = None,
    letters: list[dict] | None = None,
    last_update_success: bool = True,
    entry_data: dict | None = None,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {"receiver": receiver or [], "sender": sender or []}
    coordinator.delivered_receiver = delivered_receiver or []
    coordinator.delivered_sender = delivered_sender or []
    coordinator.letters = letters or []
    coordinator.last_update_success = last_update_success

    entry = MagicMock()
    entry.data = entry_data or {
        "username": "user@example.com",
        "password": "secret",
        "token": {"access_token": "tok", "refresh_token": "ref"},
    }
    entry.options = {"delivered_filter_type": "days", "delivered_filter_amount": 7}
    entry.runtime_data = PostNLData(
        auth=MagicMock(),
        coordinator=coordinator,
        userinfo={"account_id": "abc", "email": "user@example.com"},
    )
    return entry


async def test_diagnostics_redacts_credentials():
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry())
    assert result["entry_data"]["username"] == REDACTED
    assert result["entry_data"]["password"] == REDACTED
    # The whole token object is redacted as a single value
    assert result["entry_data"]["token"] == REDACTED


async def test_diagnostics_redacts_userinfo():
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry())
    assert result["userinfo"]["account_id"] == REDACTED
    assert result["userinfo"]["email"] == REDACTED


async def test_diagnostics_redacts_parcel_pii():
    entry = _entry(
        receiver=[{
            "barcode": "3SABC",
            "key": "3SABC-NL-1234AB",
            "source_display_name": "Brand",
            "receiver_title": "Peter",
            "receiver": "Jane Doe",
            "url": "https://jouw.postnl.nl/track-and-trace/3SABC-NL-1234AB",
            "status_message": "ON_THE_WAY",
        }],
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    parcel = result["receiver"][0]
    assert parcel["barcode"] == REDACTED
    assert parcel["key"] == REDACTED
    assert parcel["receiver_title"] == REDACTED
    # receiver's full name and the track-and-trace URL (barcode + postcode) are PII too (#18)
    assert parcel["receiver"] == REDACTED
    assert parcel["url"] == REDACTED
    # Sender brand name is not PII; status is not PII either
    assert parcel["status_message"] == "ON_THE_WAY"


async def test_diagnostics_reports_counts_and_options():
    entry = _entry(
        receiver=[{"barcode": "A"}, {"barcode": "B"}],
        sender=[{"barcode": "C"}],
        delivered_receiver=[{"barcode": "D"}],
        delivered_sender=[{"barcode": "E"}],
        letters=[{"id": "L1"}, {"id": "L2"}, {"id": "L3"}],
        last_update_success=False,
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["counts"] == {
        "receiver": 2,
        "sender": 1,
        "delivered_receiver": 1,
        "delivered_sender": 1,
        "letters": 3,
    }
    assert result["delivered_sender"][0]["barcode"] == "**REDACTED**"
    assert result["last_update_success"] is False
    assert result["entry_options"]["delivered_filter_type"] == "days"


def test_to_redact_includes_pii_keys():
    for key in ("username", "password", "token", "access_token", "email", "barcode", "postalCode"):
        assert key in TO_REDACT

"""Tests for the contents action, the overlay and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.postnl import async_unload_entry
from custom_components.postnl.const import DOMAIN
from custom_components.postnl.coordinator import PostNLCoordinator, _apply_contents

AUTH = "custom_components.postnl.AsyncConfigEntryAuth.check_and_refresh_token"
USERINFO = "custom_components.postnl.PostNLLoginAPI.userinfo"
SHIPMENTS = "custom_components.postnl.coordinator.PostNLGraphql.shipments"
LETTERS = "custom_components.postnl.coordinator.PostNLJouwAPI.letters"
REFRESH = f"{PostNLCoordinator.__module__}.PostNLCoordinator.async_request_refresh"
STORE_KEY = f"{DOMAIN}.contents.%s"

LISTED = "3SLISTED1"
OTHER = "3SOTHER2"
SENT = "3SSENT3"
UNTRACKED = "3SZZZ555"


def _shipment(barcode: str) -> dict:
    return {
        "key": f"{barcode}-NL-1234AB",
        "barcode": barcode,
        "title": "Example Shop",
        "receiverTitle": "Example Receiver",
        "delivered": True,
        "deliveredTimeStamp": dt_util.utcnow().isoformat(),
    }


def _shipments(received=(LISTED,), sent=()) -> MagicMock:
    return MagicMock(
        return_value={
            "trackedShipments": {
                "receiverShipments": [_shipment(code) for code in received],
                "senderShipments": [_shipment(code) for code in sent],
            }
        }
    )


@contextmanager
def _carrier(received=(LISTED,), sent=()):
    with (
        patch(AUTH, new=AsyncMock(return_value="tok")),
        patch(USERINFO, new=MagicMock(return_value={"account_id": "abc"})),
        patch(SHIPMENTS, new=_shipments(received, sent)),
        patch(LETTERS, new=MagicMock(return_value={"screen": {"sections": []}})),
    ):
        yield


def _config_entry(name: str = "user@example.test") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        unique_id=name,
        data={
            CONF_USERNAME: name,
            CONF_PASSWORD: "secret",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_at": 9_999_999_999,
            },
        },
        options={"delivered_filter_type": "days", "delivered_filter_amount": 7},
    )


async def _setup(hass, entry=None, received=(LISTED,), sent=()) -> MockConfigEntry:
    entry = entry or _config_entry()
    entry.add_to_hass(hass)
    with _carrier(received, sent):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, data: dict) -> None:
    with _carrier():
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


async def _call_quiet(hass, data: dict) -> AsyncMock:
    with patch(REFRESH, new=AsyncMock()) as refresh:
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        await hass.async_block_till_done()
    return refresh


def _contents(entry: MockConfigEntry, code: str, side: str = "receiver") -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data[side]}
    return parcels[code]["contents"]


def _stored(entry: MockConfigEntry, code: str) -> str | None:
    return entry.runtime_data.coordinator.contents_store.get(code)


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass)
    assert _contents(entry, LISTED) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass)

    await _call(hass, {"barcode": LISTED, "contents": " Running  shoes "})
    assert _contents(entry, LISTED) == "Running shoes"

    await _call(hass, {"barcode": LISTED, "contents": "Winter socks"})
    assert _contents(entry, LISTED) == "Winter socks"

    await _call(hass, {"barcode": LISTED, "contents": "  "})
    assert _contents(entry, LISTED) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass)
    await _call(hass, {"barcode": LISTED, "contents": "Running shoes"})

    await _call(hass, {"barcode": LISTED})

    assert _contents(entry, LISTED) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass)
    await _call(hass, {"barcode": f" {LISTED.lower()} ", "contents": "Shoes"})
    assert _contents(entry, LISTED) == "Shoes"


async def test_update_contents_works_for_a_code_the_account_does_not_list(hass):
    entry = await _setup(hass)
    await _call(hass, {"barcode": UNTRACKED, "contents": "Running shoes"})

    assert _stored(entry, UNTRACKED) == "Running shoes"


async def test_update_contents_refreshes_only_when_something_changed(hass):
    await _setup(hass)
    data = {"barcode": LISTED, "contents": "Running shoes"}
    with patch(REFRESH, new=AsyncMock()) as refresh:
        await _call(hass, data)
        assert refresh.await_count == 1
        await _call(hass, data)
        assert refresh.await_count == 1
        await _call(hass, {"barcode": UNTRACKED})
        assert refresh.await_count == 1


async def test_update_contents_reaches_sent_shipments(hass):
    entry = await _setup(hass, sent=(SENT,))
    with _carrier(sent=(SENT,)):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {"barcode": SENT, "contents": "Return parcel"},
            blocking=True,
        )
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert _contents(entry, SENT, "sender") == "Return parcel"


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, {"barcode": "  "})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    try:
        with pytest.raises(ServiceValidationError):
            await _call(hass, {"barcode": LISTED, "contents": "Shoes"})
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_the_action_goes_with_the_last_loaded_account(hass):
    first = await _setup(hass)
    second = await _setup(hass, _config_entry("other@example.test"))

    assert await hass.config_entries.async_unload(first.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")

    assert await hass.config_entries.async_unload(second.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_failed_unload_keeps_the_action(hass):
    entry = await _setup(hass)
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    assert hass.services.has_service(DOMAIN, "update_contents")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass)
    await _call(hass, {"barcode": LISTED, "contents": "Running shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _carrier():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, LISTED) == "Running shoes"


async def test_an_entry_without_a_store_file_works(hass, hass_storage):
    entry = await _setup(hass)

    assert STORE_KEY % entry.entry_id not in hass_storage
    assert _contents(entry, LISTED) is None


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass)
    await _call(hass, {"barcode": LISTED, "contents": "Running shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass)
    events = []
    for name in ("registered", "status_changed", "delivered"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", events.append)

    await _call(hass, {"barcode": LISTED, "contents": "Running shoes"})

    assert events == []


async def test_user_contents_reaches_the_delivered_list(hass):
    entry = await _setup(hass)
    entry.runtime_data.coordinator.contents_store.async_set(LISTED, "Running shoes")
    with _carrier():
        await entry.runtime_data.coordinator.async_refresh()

    assert entry.runtime_data.coordinator.delivered_receiver[0]["contents"] == (
        "Running shoes"
    )


def test_the_overlay_leaves_the_carrier_value_and_the_input_alone():
    store = MagicMock()
    parcel = {"barcode": LISTED, "contents": "Carrier text"}

    store.get.return_value = None
    assert _apply_contents(store, parcel) is parcel

    store.get.return_value = "Running shoes"
    overlaid = _apply_contents(store, parcel)
    assert overlaid["contents"] == "Running shoes"
    assert parcel["contents"] == "Carrier text"

    assert _apply_contents(store, {"barcode": None}) == {"barcode": None}


async def test_known_codes_list_both_sides(hass):
    entry = await _setup(hass, received=(LISTED,), sent=(SENT,))
    assert entry.runtime_data.coordinator.known_codes == {LISTED, SENT}


# ---------------------------------------------------------------------------
# several accounts
# ---------------------------------------------------------------------------


async def _two_accounts(hass, first_codes, second_codes, second_sent=()):
    first = await _setup(hass, received=first_codes)
    second = await _setup(
        hass, _config_entry("other@example.test"), second_codes, second_sent
    )
    return first, second


async def test_several_accounts_store_in_the_one_that_lists_the_code(hass):
    first, second = await _two_accounts(hass, (LISTED,), (OTHER,))

    refresh = await _call_quiet(hass, {"barcode": OTHER, "contents": "Shoes"})

    assert _stored(first, OTHER) is None
    assert _stored(second, OTHER) == "Shoes"
    assert refresh.await_count == 1


async def test_several_accounts_find_a_sent_shipment(hass):
    first, second = await _two_accounts(hass, (LISTED,), (), (SENT,))

    await _call_quiet(hass, {"barcode": SENT, "contents": "Shoes"})

    assert _stored(first, SENT) is None
    assert _stored(second, SENT) == "Shoes"


async def test_a_code_in_two_accounts_is_stored_in_both(hass):
    first, second = await _two_accounts(hass, (LISTED,), (LISTED,))

    refresh = await _call_quiet(hass, {"barcode": LISTED, "contents": "Shoes"})

    assert _stored(first, LISTED) == "Shoes"
    assert _stored(second, LISTED) == "Shoes"
    assert refresh.await_count == 2


async def test_several_accounts_refresh_only_where_the_value_changed(hass):
    first, second = await _two_accounts(hass, (LISTED,), (LISTED,))
    first.runtime_data.coordinator.contents_store.async_set(LISTED, "Shoes")

    refresh = await _call_quiet(hass, {"barcode": LISTED, "contents": "Shoes"})

    assert _stored(second, LISTED) == "Shoes"
    assert refresh.await_count == 1


async def test_an_unknown_code_with_several_accounts_needs_an_account(hass):
    first, second = await _two_accounts(hass, (LISTED,), (LISTED,))

    with pytest.raises(ServiceValidationError) as raised:
        await _call_quiet(hass, {"barcode": UNTRACKED, "contents": "Shoes"})

    assert raised.value.translation_key == "update_contents_entry_required"
    assert _stored(first, UNTRACKED) is None
    assert _stored(second, UNTRACKED) is None


async def test_a_named_account_stores_even_an_unknown_code(hass):
    first, second = await _two_accounts(hass, (LISTED,), (LISTED,))

    await _call_quiet(
        hass,
        {
            "barcode": UNTRACKED,
            "contents": "Shoes",
            "config_entry_id": second.entry_id,
        },
    )

    assert _stored(first, UNTRACKED) is None
    assert _stored(second, UNTRACKED) == "Shoes"


async def test_a_named_account_limits_a_code_both_accounts_list(hass):
    first, second = await _two_accounts(hass, (LISTED,), (LISTED,))

    await _call_quiet(
        hass,
        {"barcode": LISTED, "contents": "Shoes", "config_entry_id": first.entry_id},
    )

    assert _stored(first, LISTED) == "Shoes"
    assert _stored(second, LISTED) is None


async def test_a_named_account_that_is_not_loaded_is_refused(hass):
    await _setup(hass)

    with pytest.raises(ServiceValidationError):
        await _call_quiet(
            hass, {"barcode": LISTED, "contents": "Shoes", "config_entry_id": "nope"}
        )


async def test_a_single_account_stores_an_unknown_code_without_naming_it(hass):
    entry = await _setup(hass)

    await _call_quiet(hass, {"barcode": UNTRACKED, "contents": "Shoes"})

    assert _stored(entry, UNTRACKED) == "Shoes"

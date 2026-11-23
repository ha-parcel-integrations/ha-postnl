"""Tests for the contents helpers and the per-entry contents store."""

from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.postnl.const import DOMAIN
from custom_components.postnl.contents import (
    CONTENTS_MAX_LEN,
    CONTENTS_STORE_MAX_CODES,
    ContentsStore,
    clean_contents,
    join_contents,
    normalize_code,
)

STORE_KEY = f"{DOMAIN}.contents.entry1"


def test_clean_contents_collapses_whitespace():
    assert clean_contents("  Running \n\t shoes  ") == "Running shoes"


def test_clean_contents_is_none_for_nothing_useful():
    assert clean_contents(None) is None
    assert clean_contents(42) is None
    assert clean_contents("") is None
    assert clean_contents(" \n\t ") is None


def test_clean_contents_drops_filler_case_insensitively():
    assert clean_contents("Consumer Goods", frozenset({"consumer goods"})) is None
    assert clean_contents("Shoes", frozenset({"consumer goods"})) == "Shoes"


def test_clean_contents_cuts_hard_at_the_cap_without_a_trailing_space():
    text = "a" * (CONTENTS_MAX_LEN - 1) + " b"
    assert clean_contents(text) == "a" * (CONTENTS_MAX_LEN - 1)
    assert len(clean_contents("x" * 500)) == CONTENTS_MAX_LEN


def test_join_contents_joins_in_order_and_drops_empties_and_repeats():
    values = ["Shoes", "", None, "Socks", "Shoes", " Socks "]
    assert join_contents(values) == "Shoes; Socks"


def test_join_contents_is_none_without_items():
    assert join_contents([]) is None
    assert join_contents(["", None, "  "]) is None


def test_join_contents_applies_the_filler_and_the_cap():
    assert join_contents(["Gift", "Shoes"], frozenset({"gift"})) == "Shoes"
    assert len(join_contents(["x" * 100, "y" * 100])) <= CONTENTS_MAX_LEN


def test_normalize_code_strips_and_uppercases():
    assert normalize_code("  ex-123 ") == "EX-123"
    assert normalize_code(123) == "123"


async def test_store_starts_empty_and_sets_replaces_clears(hass):
    store = ContentsStore(hass, "entry1")
    await store.async_load()
    assert store.get("POSTNLCODE1") is None

    assert store.async_set("postnlcode1", "  Running   shoes ") is True
    assert store.get(" POSTNLCODE1 ") == "Running shoes"
    assert store.async_set("POSTNLCODE1", "Running shoes") is False
    assert store.async_set("POSTNLCODE1", "Socks") is True
    assert store.get("POSTNLCODE1") == "Socks"
    assert store.async_set("POSTNLCODE1", "  ") is True
    assert store.get("POSTNLCODE1") is None
    assert store.async_set("POSTNLCODE1", None) is False


async def test_store_persists_and_reloads(hass, hass_storage):
    store = ContentsStore(hass, "entry1")
    await store.async_load()
    store.async_set("POSTNLCODE1", "Running shoes")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert hass_storage[STORE_KEY]["version"] == 1
    assert hass_storage[STORE_KEY]["data"] == {
        "by_code": {"POSTNLCODE1": "Running shoes"}
    }

    reloaded = ContentsStore(hass, "entry1")
    await reloaded.async_load()
    assert reloaded.get("POSTNLCODE1") == "Running shoes"


async def test_store_ignores_malformed_stored_data(hass, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"by_code": {"EX1": "Shoes", "EX2": "", "EX3": 7}},
    }
    store = ContentsStore(hass, "entry1")
    await store.async_load()
    assert store.get("EX1") == "Shoes"
    assert store.get("EX2") is None
    assert store.get("EX3") is None

    hass_storage[STORE_KEY]["data"] = {"by_code": ["not", "a", "dict"]}
    broken = ContentsStore(hass, "entry1")
    await broken.async_load()
    assert broken.get("EX1") is None


async def test_store_drops_the_oldest_code_past_the_cap(hass):
    store = ContentsStore(hass, "entry1")
    await store.async_load()
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"CODE{number}", f"Item {number}")

    assert store.get("CODE0") is None
    assert store.get("CODE1") == "Item 1"
    assert store.get(f"CODE{CONTENTS_STORE_MAX_CODES}") is not None


async def test_store_remove_deletes_the_file(hass, hass_storage):
    store = ContentsStore(hass, "entry1")
    await store.async_load()
    store.async_set("POSTNLCODE1", "Running shoes")
    await store._store.async_save(store._data_to_save())
    assert STORE_KEY in hass_storage

    await store.async_remove()

    assert STORE_KEY not in hass_storage
    assert store.get("POSTNLCODE1") is None

"""Tests for the PostNL letter image entity.

Covers the PostNLLetterImage class properties + caching + sync logic
without the full HA platform setup (which is exercised separately by
test_init.py's setup/unload). _sync_letters is tested by invoking the
exported async_setup_entry's inner callback indirectly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.postnl.image import PostNLLetterImage, async_setup_entry

from .payloads import letter_sample

_USERINFO = {"account_id": "abc-123", "email": "user@example.com"}


def _coordinator(letters: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.letters = letters or []
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    coordinator.last_update_success = True
    coordinator.jouw_api = MagicMock()
    # async_image() must go through this (token-refresh-before-use), never
    # read coordinator.jouw_api directly — see test_async_image_refreshes_token_before_fetch.
    coordinator.async_get_jouw_api = AsyncMock(return_value=coordinator.jouw_api)
    return coordinator


def _entry(coordinator: MagicMock, userinfo: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.runtime_data = MagicMock()
    entry.runtime_data.coordinator = coordinator
    entry.runtime_data.userinfo = userinfo or _USERINFO
    entry.async_on_unload = MagicMock()
    return entry


def _letter(letter_id: str = "L1", *, title: str = "16 juni",
            image_url: str | None = "https://example.com/img1") -> dict:
    return letter_sample(
        letter_id, title, unread=False, image_url=image_url, date=None
    )


# ---------------------------------------------------------------------------
# PostNLLetterImage
# ---------------------------------------------------------------------------


def _make_image(coordinator, letter_id="L1"):
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    img = PostNLLetterImage(hass, coordinator, _entry(coordinator), _USERINFO, letter_id)
    # The base Entity.hass is normally set when the platform adds the entity;
    # in unit tests we set it manually so async_image() can dispatch.
    img.hass = hass
    return img, hass


def test_letter_image_attributes_set_from_letter():
    img, _ = _make_image(_coordinator([_letter(title="20 juni")]), letter_id="L1")
    assert img.unique_id == "abc-123_letter_image_L1"
    assert img.translation_placeholders == {"title": "20 juni"}
    assert img._image_url == "https://example.com/img1"


def test_letter_image_title_falls_back_to_id_when_missing():
    img, _ = _make_image(_coordinator([{"id": "L1", "image_url": "x"}]), letter_id="L1")
    assert img.translation_placeholders == {"title": "L1"}


def test_letter_image_state_uses_parsed_letter_date():
    # Regression: previously the entity state was always dt_util.utcnow(),
    # so every HA restart showed the boot time instead of the letter date.
    letter = _letter("L1")
    letter["date"] = "2026-06-16"
    img, _ = _make_image(_coordinator([letter]), letter_id="L1")
    assert img.image_last_updated.year == 2026
    assert img.image_last_updated.month == 6
    assert img.image_last_updated.day == 16


def test_letter_image_state_falls_back_to_now_when_date_unparseable():
    # When parse_letter_date couldn't extract anything from the title we
    # still want a sensible "last updated" instead of crashing or None.
    letter = _letter("L1", title="???")
    letter["date"] = None
    img, _ = _make_image(_coordinator([letter]), letter_id="L1")
    assert img.image_last_updated is not None


def test_letter_image_attributes_mirror_letter_dict():
    letter = _letter("L1", title="16 juni")
    letter["date"] = "2026-06-16"
    letter["unread"] = True
    img, _ = _make_image(_coordinator([letter]), letter_id="L1")
    assert img.extra_state_attributes == {
        "id": "L1",
        "title": "16 juni",
        "date": "2026-06-16",
        "unread": True,
    }


def test_letter_image_attributes_are_none_when_letter_missing():
    coordinator = _coordinator([_letter("L1")])
    img, _ = _make_image(coordinator, letter_id="L1")
    coordinator.letters = []
    assert img.extra_state_attributes == {
        "id": None,
        "title": None,
        "date": None,
        "unread": None,
    }


def test_letter_image_unavailable_when_letter_gone():
    coordinator = _coordinator([_letter("L1")])
    img, _ = _make_image(coordinator, letter_id="L1")
    # Drop the letter; the entity should be unavailable.
    coordinator.letters = []
    assert img.available is False


def test_letter_image_handle_coordinator_update_resets_cache_when_url_changes(monkeypatch):
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/old")])
    img, _ = _make_image(coordinator, letter_id="L1")
    img._cached_url = "https://example.com/old"
    img._cached_bytes = b"old"
    # Skip the base class's HA state write — we only care about cache state here.
    monkeypatch.setattr(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity._handle_coordinator_update",
        lambda self: None,
    )

    coordinator.letters = [_letter("L1", image_url="https://example.com/new")]
    img._handle_coordinator_update()

    assert img._image_url == "https://example.com/new"
    assert img._cached_url is None
    assert img._cached_bytes is None


def test_letter_image_handle_coordinator_update_keeps_cache_when_url_same(monkeypatch):
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/img")])
    img, _ = _make_image(coordinator, letter_id="L1")
    img._cached_url = "https://example.com/img"
    img._cached_bytes = b"cached"
    monkeypatch.setattr(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity._handle_coordinator_update",
        lambda self: None,
    )

    img._handle_coordinator_update()

    assert img._cached_url == "https://example.com/img"
    assert img._cached_bytes == b"cached"


@pytest.mark.asyncio
async def test_async_image_returns_none_when_letter_missing():
    coordinator = _coordinator([])
    img, _ = _make_image(coordinator, letter_id="L1")
    assert await img.async_image() is None


@pytest.mark.asyncio
async def test_async_image_returns_cached_bytes_when_url_matches():
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/img")])
    img, hass = _make_image(coordinator, letter_id="L1")
    img._cached_url = "https://example.com/img"
    img._cached_bytes = b"cached"

    assert await img.async_image() == b"cached"
    hass.async_add_executor_job.assert_not_called()


@pytest.mark.asyncio
async def test_async_image_fetches_via_executor_and_caches_result():
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/img")])
    img, hass = _make_image(coordinator, letter_id="L1")
    hass.async_add_executor_job = AsyncMock(return_value=(b"fresh", "image/png"))

    result = await img.async_image()

    assert result == b"fresh"
    assert img._cached_bytes == b"fresh"
    assert img._cached_url == "https://example.com/img"
    assert img.content_type == "image/png"


@pytest.mark.asyncio
async def test_async_image_returns_none_when_executor_raises():
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/img")])
    img, hass = _make_image(coordinator, letter_id="L1")
    hass.async_add_executor_job = AsyncMock(side_effect=RuntimeError("boom"))

    assert await img.async_image() is None
    # Cache should still be unset so the next attempt will retry.
    assert img._cached_bytes is None


@pytest.mark.asyncio
async def test_async_image_refreshes_token_before_fetch():
    # Regression test: async_image() used to read coordinator.jouw_api
    # directly, so a bearer token that expired between polls (default poll
    # interval 30 min; PostNL's own access tokens are not guaranteed to live
    # that long) produced a 401 on every fetch until the next poll happened to
    # refresh it — see CLAUDE.md and PostNLCoordinator.async_get_jouw_api.
    coordinator = _coordinator([_letter("L1", image_url="https://example.com/img")])
    img, hass = _make_image(coordinator, letter_id="L1")
    hass.async_add_executor_job = AsyncMock(return_value=(b"fresh", "image/png"))

    await img.async_image()

    coordinator.async_get_jouw_api.assert_awaited_once()
    hass.async_add_executor_job.assert_called_once_with(
        coordinator.jouw_api.image, "https://example.com/img"
    )


# ---------------------------------------------------------------------------
# async_setup_entry — covers _sync_letters add + remove paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_adds_initial_letter_images(hass):
    coordinator = _coordinator([_letter("L1"), _letter("L2"), {"id": "L3"}])  # L3 has no image_url
    entry = _entry(coordinator)
    added: list = []

    def _add(generator):
        for ent in generator:
            added.append(ent)

    await async_setup_entry(hass, entry, _add)

    assert {e._letter_id for e in added} == {"L1", "L2"}
    coordinator.async_add_listener.assert_called_once()


@pytest.mark.asyncio
async def test_async_setup_entry_listener_adds_new_and_removes_stale(hass):
    coordinator = _coordinator([_letter("L1")])
    entry = _entry(coordinator)
    added: list = []

    def _add(gen):
        for ent in gen:
            added.append(ent)

    listener_holder: dict = {}

    def _capture_listener(fn):
        listener_holder["fn"] = fn
        return lambda: None

    coordinator.async_add_listener = MagicMock(side_effect=_capture_listener)

    await async_setup_entry(hass, entry, _add)
    initial_count = len(added)

    # Coordinator update: L1 removed, L2 added.
    coordinator.letters = [_letter("L2")]
    listener_holder["fn"]()

    new_ids = {e._letter_id for e in added[initial_count:]}
    assert new_ids == {"L2"}

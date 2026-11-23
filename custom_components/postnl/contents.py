"""Parcel contents: the cleaning helpers and the per-entry store of user values."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

CONTENTS_MAX_LEN = 120
CONTENTS_STORE_VERSION = 1
CONTENTS_STORE_MAX_CODES = 200


def clean_contents(value: object, filler: frozenset[str] = frozenset()) -> str | None:
    """Return ``value`` as one trimmed line, or ``None`` when it says nothing."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or text.casefold() in filler:
        return None
    return text[:CONTENTS_MAX_LEN].rstrip()


def join_contents(
    values: Iterable[object], filler: frozenset[str] = frozenset()
) -> str | None:
    """Join several item texts as ``"A; B"``, dropping empties and repeats."""
    seen: list[str] = []
    for value in values:
        cleaned = clean_contents(value, filler)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return clean_contents("; ".join(seen)) if seen else None


def normalize_code(code: object) -> str:
    """Return the key under which a tracking code is stored and looked up."""
    return str(code).strip().upper()


class ContentsStore:
    """The contents a user set per tracking code, kept in one file per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store for one config entry."""
        self._store: Store[dict[str, dict[str, str]]] = Store(
            hass, CONTENTS_STORE_VERSION, f"{DOMAIN}.contents.{entry_id}"
        )
        self._by_code: dict[str, str] = {}

    async def async_load(self) -> None:
        """Read the stored values; an entry that never had any reads as empty."""
        data = await self._store.async_load()
        by_code = data.get("by_code") if isinstance(data, dict) else None
        self._by_code = {}
        if not isinstance(by_code, dict):
            return
        for code, text in by_code.items():
            cleaned = clean_contents(text)
            if cleaned:
                self._by_code[normalize_code(code)] = cleaned

    def get(self, code: object) -> str | None:
        """Return the value for ``code``, or ``None``."""
        return self._by_code.get(normalize_code(code))

    @callback
    def async_set(self, code: object, text: str | None) -> bool:
        """Set, replace or (with ``None``) clear a value; True when it changed."""
        key = normalize_code(code)
        cleaned = clean_contents(text)
        if cleaned is None:
            if key not in self._by_code:
                return False
            del self._by_code[key]
        else:
            if self._by_code.get(key) == cleaned:
                return False
            self._by_code[key] = cleaned
            while len(self._by_code) > CONTENTS_STORE_MAX_CODES:
                del self._by_code[next(iter(self._by_code))]
        self._store.async_delay_save(self._data_to_save, 1)
        return True

    @callback
    def _data_to_save(self) -> dict[str, dict[str, str]]:
        return {"by_code": dict(self._by_code)}

    async def async_remove(self) -> None:
        """Delete the file; used when the config entry is removed."""
        self._by_code = {}
        await self._store.async_remove()

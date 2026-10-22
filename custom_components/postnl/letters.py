"""Pure MyMail letter parsing helpers for PostNL.

No I/O and no Home Assistant objects: this is the letter-extraction logic for
the server-driven-UI MyMail response, kept apart from the coordinator (fetching,
caching, events) so it stays trivially unit-testable. Letters are a
PostNL-specific concept — they have no tracking status or canonical parcel
shape — so they live here rather than in ``parcels.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

_DUTCH_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


def parse_letter_date(title: str, *, today: datetime | None = None) -> str | None:
    """Convert a Dutch day-month title like '16 juni' into an ISO date string.

    The MyMail endpoint returns dates without a year. We infer the year from
    ``today``: if the parsed month/day is more than 31 days ahead of today, it
    must belong to the previous year (PostNL only retains 2 weeks of mail).
    """
    if not title:
        return None
    parts = title.strip().lower().split()
    if len(parts) != 2:
        return None
    try:
        day = int(parts[0])
    except ValueError:
        return None
    month = _DUTCH_MONTHS.get(parts[1])
    if month is None:
        return None
    now = (today or datetime.now(UTC)).date()
    try:
        candidate = now.replace(month=month, day=day)
    except ValueError:
        return None
    if (candidate - now).days > 31:
        try:
            candidate = candidate.replace(year=candidate.year - 1)
        except ValueError:
            return None
    return candidate.isoformat()


def extract_letters(payload: dict, *, today: datetime | None = None) -> list[dict]:
    """Extract letter entries from the server-driven-UI MyMail response."""
    sections = ((payload or {}).get("screen") or {}).get("sections") or []
    letters: list[dict] = []
    for section in sections:
        for item in section.get("items") or []:
            if item.get("type") != "Letter":
                continue
            title = item.get("title")
            letters.append(
                {
                    "id": item.get("editId"),
                    "title": title,
                    "date": parse_letter_date(title, today=today),
                    "unread": bool(item.get("isUnread")),
                    "image_url": (item.get("image") or {}).get("url"),
                }
            )
    return letters

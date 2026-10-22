"""Sample PostNL payloads shared by the test modules.

Covers both sides of the fork: the raw GraphQL/track & trace shapes the API
returns, and the normalised letter dict the letters pipeline hands to the image
platform. Keep them here rather than inline in each test module: when a shape
turns out to differ from what we assumed, there is then exactly one place to
fix — ``letter_sample`` in particular was previously duplicated between the
coordinator and image tests.

Every helper returns a **fresh** dict, so a test may mutate what it gets back
without leaking into the next one.
"""

from __future__ import annotations

BARCODE = "3SABC"


def observations() -> list[dict]:
    """``analyticsInfo.allObservations`` — PostNL's own event timeline."""
    return [
        {
            "observationDate": "2026-05-21T14:41:45.943+02:00",
            "observationCode": "A01",
            "description": "Pakket is nog niet ontvangen",
        },
        {
            "observationDate": "2026-05-21T20:22:11+02:00",
            "observationCode": "B01",
            "description": "Pakket is ontvangen door PostNL",
        },
        {
            "observationDate": "2026-05-22T10:06:45+02:00",
            "observationCode": "J01",
            "description": "Zending is gesorteerd",
        },
        {
            "observationDate": "2026-05-22T11:01:21+02:00",
            "observationCode": "J05",
            "description": "Bezorger is onderweg",
        },
    ]


def track_and_trace(barcode: str = BARCODE) -> dict:
    """A track & trace response for an out-for-delivery parcel."""
    return {
        "colli": {
            barcode: {
                "statusPhase": {"message": "Bezorger is onderweg"},
                "analyticsInfo": {"allObservations": observations()},
            }
        }
    }


def sdui_payload(letters: list[dict]) -> dict:
    """The SDUI screen envelope the letters are extracted from.

    The two ``List`` sections are noise on purpose: extraction must pick the
    ``Grid`` and ignore the rest.
    """
    return {
        "screen": {
            "sections": [
                {"type": "List", "items": [{"type": "Text"}]},  # ignored
                {"type": "Grid", "items": list(letters)},
                {"type": "List", "items": [{"type": "Default"}]},  # ignored
            ]
        }
    }


def letter_sample(
    letter_id: str = "L1",
    title: str = "16 juni",
    *,
    unread: bool = True,
    image_url: str | None = "https://example.com/a.jpg",
    date: str | None = "2026-06-16",
) -> dict:
    """A **normalised** letter, as the letters pipeline yields it."""
    return {
        "id": letter_id,
        "title": title,
        "date": date,
        "unread": unread,
        "image_url": image_url,
    }

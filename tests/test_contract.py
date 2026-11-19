"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.postnl.const import ParcelStatus
from custom_components.postnl.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _obs(code: str, stamp: str = "2099-05-21T20:22:11+02:00") -> dict:
    return {"observationDate": stamp, "observationCode": code, "description": "x"}


def _sample(codes=("J05",), **over) -> dict:
    parcel = {
        "barcode": "3SEXAMPLE",
        "name": "Example Shop",
        "delivered": False,
        "status_message": "Bezorger is onderweg",
        "delivery_address_type": "Home",
        "planned_from": "2099-05-22T13:00:00+02:00",
        "planned_to": "2099-05-22T15:00:00+02:00",
        "observations": [
            _obs(code, f"2099-05-2{i + 1}T10:00:00+02:00")
            for i, code in enumerate(codes)
        ],
    }
    parcel.update(over)
    return parcel


SAMPLES = {
    "active": lambda: _sample(),
    "at_pickup_point": lambda: _sample(("J02",), delivery_address_type="ServicePoint"),
    "collected": lambda: _sample(
        ("J02", "I02"), delivered=True, delivery_address_type="ServicePoint"
    ),
    "collected_no_flag": lambda: _sample(("J02", "I02")),
    "delivered": lambda: _sample(("I01",), delivered=True),
    "returning": lambda: _sample(("H04",)),
    "moment_only": lambda: _sample(planned_to=None),
    "date_only": lambda: _sample(planned_from="2099-05-22", planned_to=None),
    "bare": lambda: {"barcode": "3SBARE"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


@pytest.mark.parametrize("name", ["collected", "collected_no_flag"])
def test_a_collected_pickup_parcel_stays_pickup(name):
    parcel = normalize_parcel(SAMPLES[name]())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = normalize_parcel(SAMPLES["moment_only"]())
    assert parcel["planned_to"] == "2099-05-22T23:59:59+02:00"


def test_a_date_covers_the_whole_day():
    parcel = normalize_parcel(SAMPLES["date_only"]())
    assert parcel["planned_from"] == "2099-05-22T00:00:00+02:00"
    assert parcel["planned_to"] == "2099-05-22T23:59:59+02:00"


def test_the_narrow_eta_still_ends_at_the_window_end():
    parcel = normalize_parcel(_sample(expected_datetime="2099-05-22T14:10:00+02:00"))
    assert parcel["planned_from"] == "2099-05-22T14:10:00+02:00"
    assert parcel["planned_to"] == "2099-05-22T15:00:00+02:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False

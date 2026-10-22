"""Tests for the PostNL deliveries calendar."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from custom_components.postnl.calendar import PostNLDeliveriesCalendar

USERINFO = {"account_id": "abc-123", "email": "user@example.com"}


def _make_coordinator(receiver: list[dict]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {"receiver": receiver}
    return coordinator


def _parcel(
    barcode: str,
    planned_from: str | None = None,
    planned_to: str | None = None,
    delivered: bool = False,
    pickup: bool = False,
    pickup_point: str | None = None,
) -> dict:
    return {
        "barcode": barcode,
        "sender": "Example Sender",
        "status": "out_for_delivery",
        "delivered": delivered,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": "https://track/123",
    }


def _calendar(receiver: list[dict]) -> PostNLDeliveriesCalendar:
    return PostNLDeliveriesCalendar(_make_coordinator(receiver), USERINFO)


def test_event_returns_earliest_upcoming():
    cal = _calendar(
        [
            _parcel("LATE", planned_from="2099-01-02T10:00:00Z"),
            _parcel("SOON", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    event = cal.event
    assert event is not None
    assert event.uid == "SOON"
    assert event.summary == "Example Sender"


def test_delivered_parcels_are_excluded():
    cal = _calendar(
        [
            _parcel("DONE", planned_from="2099-01-01T10:00:00Z", delivered=True),
        ]
    )
    assert cal.event is None


def test_event_none_when_no_planned_parcels():
    cal = _calendar([_parcel("NOPLAN")])
    assert cal.event is None


def test_moment_gets_one_hour_duration():
    cal = _calendar([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert len(events) == 1
    assert events[0].end == datetime(2099, 1, 1, 11, 0, tzinfo=UTC)


def test_interval_uses_window():
    cal = _calendar(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                planned_to="2099-01-01T12:00:00Z",
            )
        ]
    )
    assert cal._events()[0].end == datetime(2099, 1, 1, 12, 0, tzinfo=UTC)


def test_pickup_parcel_sets_location():
    cal = _calendar(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                pickup=True,
                pickup_point="PostNL Punt",
            )
        ]
    )
    assert cal._events()[0].location == "PostNL Punt"


async def test_get_events_filters_by_range():
    cal = _calendar(
        [
            _parcel("PAST", planned_from="2000-01-01T10:00:00Z"),
            _parcel("FUTURE", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    start = datetime(2098, 1, 1, tzinfo=UTC)
    end = datetime(2100, 1, 1, tzinfo=UTC)
    events = await cal.async_get_events(MagicMock(), start, end)
    assert {e.uid for e in events} == {"FUTURE"}

"""Tests for the PostNL coordinator helpers and transform_shipment."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from custom_components.postnl.const import (
    CAPABILITIES,
    CONF_INCLUDE_HISTORY,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.postnl.coordinator import (
    PostNLCoordinator,
    _refresh_interval,
)
from custom_components.postnl.letters import (
    _DUTCH_MONTHS,
    extract_letters,
    parse_letter_date,
)
from custom_components.postnl.parcels import (
    _convert_native_dimensions,
    _delivery_dt,
    _extract_observations,
    build_history,
    map_observation_status,
    map_parcel_status,
    normalize_parcel,
    sort_parcels_by_ts,
)

from .payloads import (
    letter_sample,
    observations,
    sdui_payload,
    track_and_trace,
)

# ---------------------------------------------------------------------------
# _delivery_dt
# ---------------------------------------------------------------------------


def test_delivery_dt_parses_iso_with_tz():
    parcel = {"delivered_at": "2026-06-12T10:00:00+02:00"}
    dt = _delivery_dt(parcel)
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 10


def test_delivery_dt_assigns_utc_when_naive():
    parcel = {"delivered_at": "2026-06-12T10:00:00"}
    dt = _delivery_dt(parcel)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.tzinfo.utcoffset(dt).total_seconds() == 0


def test_delivery_dt_handles_z_suffix():
    parcel = {"delivered_at": "2026-06-12T10:00:00Z"}
    dt = _delivery_dt(parcel)
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_delivery_dt_returns_none_for_missing():
    assert _delivery_dt({}) is None
    assert _delivery_dt({"delivered_at": None}) is None
    assert _delivery_dt({"delivered_at": ""}) is None


def test_delivery_dt_returns_none_for_garbage():
    assert _delivery_dt({"delivered_at": "not a date"}) is None


# ---------------------------------------------------------------------------
# map_parcel_status
# ---------------------------------------------------------------------------


def test_map_parcel_status_delivered_flag_short_circuits():
    assert map_parcel_status({"delivered": True, "status_message": "anything"}) == ParcelStatus.DELIVERED


def test_map_parcel_status_unknown_when_message_missing():
    assert map_parcel_status({}) == ParcelStatus.UNKNOWN
    assert map_parcel_status({"status_message": ""}) == ParcelStatus.UNKNOWN
    assert map_parcel_status({"status_message": None}) == ParcelStatus.UNKNOWN


def test_map_parcel_status_out_for_delivery_beats_in_transit():
    # "onderweg naar het bezorgadres" contains "onderweg" but must be more specific
    assert map_parcel_status({"status_message": "Pakket is onderweg naar het bezorgadres"}) == ParcelStatus.OUT_FOR_DELIVERY


def test_map_parcel_status_wordt_vandaag_bezorgd_is_out_for_delivery():
    # "wordt vandaag bezorgd" contains "bezorgd" but must NOT match DELIVERED
    assert map_parcel_status({"status_message": "Pakket wordt vandaag bezorgd"}) == ParcelStatus.OUT_FOR_DELIVERY


def test_map_parcel_status_in_transit_for_onderweg():
    assert map_parcel_status({"status_message": "Pakket is onderweg"}) == ParcelStatus.IN_TRANSIT


def test_map_parcel_status_failed_delivery_today_is_in_transit():
    # Reported in issue #6: a delayed/failed attempt maps to IN_TRANSIT,
    # mirroring the G01/G05/T04 observation codes.
    message = (
        "Sorry, bezorgmoment is bijgewerkt. "
        "Het lukt vandaag niet je pakket te bezorgen."
    )
    assert map_parcel_status({"status_message": message}) == ParcelStatus.IN_TRANSIT


def test_map_parcel_status_busy_delay_is_in_transit():
    # Reported in issue #12: a "delivery takes a bit longer due to busyness"
    # notice is just a delay — still IN_TRANSIT.
    message = (
        "Door drukte duurt de bezorging wat langer dan normaal. "
        "We bezorgen je pakket zo snel mogelijk. "
    )
    assert map_parcel_status({"status_message": message}) == ParcelStatus.IN_TRANSIT


def test_map_parcel_status_at_pickup_point_for_postnl_punt():
    assert map_parcel_status({"status_message": "Pakket ligt klaar bij PostNL punt"}) == ParcelStatus.AT_PICKUP_POINT


def test_map_parcel_status_registered_for_aangemeld():
    assert map_parcel_status({"status_message": "Pakket is aangemeld"}) == ParcelStatus.REGISTERED


def test_map_parcel_status_unknown_for_unmapped_string():
    assert map_parcel_status({"status_message": "Verstuurd via warpdrive"}) == ParcelStatus.UNKNOWN


def test_map_parcel_status_literal_unknown_is_recognised(caplog):
    # PostNL itself reports "Unknown" for a not-yet-tracked parcel; treat it as
    # UNKNOWN without the "help us map it" warning (#9).
    assert map_parcel_status({"status_message": "Unknown"}) == ParcelStatus.UNKNOWN
    assert "issues/new" not in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------


def test_normalize_parcel_canonical_top_level_keys():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "source_display_name": "Bol.com",
        "url": "https://example.com",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "delivery_date": None,
        "delivery_address_type": "Recipient",
        "planned_from": "2026-06-20T09:00:00Z",
        "planned_to": "2026-06-20T17:00:00Z",
    })
    assert parcel["carrier"] == "PostNL"
    assert parcel["barcode"] == "3SXYZ"
    assert parcel["sender"] == "Bol.com"
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Pakket is onderweg"
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] == "2026-06-20T09:00:00Z"
    assert parcel["planned_to"] == "2026-06-20T17:00:00Z"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"] == "https://example.com"
    assert "status_message" not in parcel  # original lives only under raw
    assert parcel["raw"]["status_message"] == "Pakket is onderweg"


def test_normalize_parcel_sender_prefers_name_over_source_display_name():
    # #13: for a parcel visible through shared visibility in the PostNL app,
    # ``sourceDisplayName`` holds the sharing housemate's name, not the shop.
    # ``name`` (the GraphQL title) is the shop in every observed payload.
    parcel = normalize_parcel({
        "barcode": "AAAA1111111111",
        "name": "Shop One",
        "source_display_name": "Account Owner A",
        "delivered": False,
        "status_message": "Pakket is onderweg",
    })
    assert parcel["sender"] == "Shop One"
    # The shared-from account stays visible under raw.
    assert parcel["raw"]["source_display_name"] == "Account Owner A"


def test_normalize_parcel_sender_falls_back_to_source_display_name():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "name": None,
        "source_display_name": "Bol.com",
        "delivered": False,
        "status_message": "Pakket is onderweg",
    })
    assert parcel["sender"] == "Bol.com"


def test_normalize_parcel_pickup_detected_for_service_point():
    parcel = normalize_parcel({
        "barcode": "X",
        "delivered": False,
        "delivery_address_type": "ServicePoint",
        "status_message": "Pakket is onderweg",
    })
    assert parcel["pickup"] is True


def test_normalize_parcel_delivered_window_cleared():
    parcel = normalize_parcel({
        "barcode": "X",
        "delivered": True,
        "delivery_date": "2026-06-20T10:00:00Z",
        "status_message": "Pakket is bezorgd",
        "planned_from": "2026-06-20T09:00:00Z",
        "planned_to": "2026-06-20T11:00:00Z",
    })
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-06-20T10:00:00Z"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_parcel_passes_receiver_through():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "receiver": "Peter",
    })
    assert parcel["receiver"] == "Peter"


def test_normalize_parcel_weight_and_dimensions_from_native():
    """Canonical weight (kg) + dimensions (cm + text) derive from native g + mm."""
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "dimensions": {"weight": 1500, "depth": 300, "width": 200, "height": 150},
    })
    assert parcel["weight"] == 1.5
    assert parcel["dimensions"] == {
        "length": 30.0,
        "width": 20.0,
        "height": 15.0,
        "text": "30 x 20 x 15 cm",
    }
    # Native dimensions stay on ``raw`` for power users.
    assert parcel["raw"]["dimensions"] == {
        "weight": 1500, "depth": 300, "width": 200, "height": 150,
    }


def test_normalize_parcel_weight_and_dimensions_none_when_native_missing():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": True,
        "status_message": "Pakket is bezorgd",
    })
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


# ---------------------------------------------------------------------------
# _convert_native_dimensions
# ---------------------------------------------------------------------------


def test_convert_native_dimensions_converts_g_to_kg_and_mm_to_cm():
    native = {"weight": 1500, "depth": 300, "width": 200, "height": 150}
    weight, canonical = _convert_native_dimensions(native)
    assert weight == 1.5
    assert canonical == {
        "length": 30.0,
        "width": 20.0,
        "height": 15.0,
        "text": "30 x 20 x 15 cm",
    }


def test_convert_native_dimensions_handles_weight_only():
    weight, canonical = _convert_native_dimensions({"weight": 800})
    assert weight == 0.8
    assert canonical is None


def test_convert_native_dimensions_returns_none_for_empty_input():
    assert _convert_native_dimensions(None) == (None, None)
    assert _convert_native_dimensions({}) == (None, None)


def test_convert_native_dimensions_rounds_text_to_integers():
    """The text variant always renders integer cm, even for fractional values."""
    native = {"weight": 100, "depth": 254, "width": 124, "height": 76}
    _, canonical = _convert_native_dimensions(native)
    assert canonical["text"] == "25 x 12 x 8 cm"


# ---------------------------------------------------------------------------
# _refresh_interval
# ---------------------------------------------------------------------------


def test_refresh_interval_defaults_to_30_minutes_when_option_unset():
    entry = MagicMock()
    entry.options = {}
    assert _refresh_interval(entry).total_seconds() == 30 * 60


def test_refresh_interval_reads_from_options():
    entry = MagicMock()
    entry.options = {"refresh_interval": 60}
    assert _refresh_interval(entry).total_seconds() == 60 * 60


# ---------------------------------------------------------------------------
# parse_letter_date
# ---------------------------------------------------------------------------


def _today(year: int = 2026, month: int = 6, day: int = 16) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_parse_letter_date_today_uses_current_year():
    assert parse_letter_date("16 juni", today=_today()) == "2026-06-16"


def test_parse_letter_date_future_month_within_window_keeps_year():
    # 1 July is 15 days ahead of 16 June → still this year
    assert parse_letter_date("1 juli", today=_today()) == "2026-07-01"


def test_parse_letter_date_far_future_rolls_back_year():
    # 30 December seen on 16 June must belong to last December
    assert parse_letter_date("30 december", today=_today()) == "2025-12-30"


def test_parse_letter_date_returns_none_for_invalid():
    assert parse_letter_date("", today=_today()) is None
    assert parse_letter_date(None, today=_today()) is None
    assert parse_letter_date("garbage", today=_today()) is None
    assert parse_letter_date("31 februari", today=_today()) is None  # 31 Feb doesn't exist
    assert parse_letter_date("12 unknownmonth", today=_today()) is None
    assert parse_letter_date("notanumber juni", today=_today()) is None


def test_dutch_months_dict_has_twelve_months():
    assert len(_DUTCH_MONTHS) == 12
    assert _DUTCH_MONTHS["januari"] == 1
    assert _DUTCH_MONTHS["december"] == 12


# ---------------------------------------------------------------------------
# extract_letters
# ---------------------------------------------------------------------------


def test_extract_letters_picks_up_letter_items():
    payload = sdui_payload([
        {
            "type": "Letter",
            "editId": "ABC1",
            "title": "16 juni",
            "isUnread": False,
            "image": {"url": "https://example.com/a"},
        },
        {
            "type": "Letter",
            "editId": "ABC2",
            "title": "15 juni",
            "isUnread": True,
            "image": {"url": "https://example.com/b"},
        },
    ])
    letters = extract_letters(payload, today=_today())
    assert len(letters) == 2
    assert letters[0]["id"] == "ABC1"
    assert letters[0]["title"] == "16 juni"
    assert letters[0]["date"] == "2026-06-16"
    assert letters[0]["unread"] is False
    assert letters[0]["image_url"] == "https://example.com/a"
    assert letters[1]["unread"] is True


def test_extract_letters_ignores_non_letter_items():
    payload = sdui_payload([{"type": "TextListItem", "title": "Header"}])
    assert extract_letters(payload, today=_today()) == []


def test_extract_letters_returns_empty_for_missing_screen():
    assert extract_letters({}, today=_today()) == []
    assert extract_letters(None, today=_today()) == []
    assert extract_letters({"screen": {}}, today=_today()) == []


def test_extract_letters_handles_missing_image_block():
    payload = sdui_payload([
        {"type": "Letter", "editId": "X", "title": "16 juni", "isUnread": False},
    ])
    letters = extract_letters(payload, today=_today())
    assert letters[0]["image_url"] is None


# ---------------------------------------------------------------------------
# transform_shipment
# ---------------------------------------------------------------------------


def _make_coordinator(hass):
    entry = MagicMock()
    entry.options = {}
    coordinator = PostNLCoordinator(hass, entry)
    coordinator.jouw_api = MagicMock()
    return coordinator


async def test_transform_shipment_short_circuits_for_delivered(hass):
    coordinator = _make_coordinator(hass)
    shipment = {
        "key": "K1",
        "barcode": "3SABC",
        "title": "Online Retailer",
        "detailsUrl": "https://example.com",
        "shipmentType": "Parcel",
        "receiverTitle": "Peter ",
        "sourceDisplayName": "Brand",
        "deliveredTimeStamp": "2026-06-15T14:00:00Z",
        "deliveryAddressType": "ADDRESS",
        "delivered": True,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["barcode"] == "3SABC"
    assert parcel["delivered"] is True
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Pakket is bezorgd"
    # #13: the title wins over sourceDisplayName on the delivered path too.
    assert parcel["sender"] == "Online Retailer"
    assert parcel["raw"]["source_display_name"] == "Brand"
    # No track_and_trace call should be made for delivered shipments
    coordinator.jouw_api.track_and_trace.assert_not_called()


async def test_transform_shipment_sender_ignores_shared_visibility_name(hass):
    # #13: housemates who share parcel visibility each see the other's parcels;
    # sourceDisplayName then holds the other account holder's name. The sender
    # must still be the shop, and the title's leading space must be stripped.
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "AAAA1111111111": {"statusPhase": {"message": "Pakket is onderweg"}}
        }
    })
    shipment = {
        "key": "K13",
        "barcode": "AAAA1111111111",
        "title": " Shop One",
        "receiverTitle": "Account Owner A",
        "sourceAccountId": "acct-a",
        "sourceDisplayName": "Account Owner A",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["sender"] == "Shop One"
    assert parcel["raw"]["name"] == "Shop One"
    assert parcel["raw"]["source_display_name"] == "Account Owner A"


async def test_transform_shipment_source_display_name_is_null_for_own_parcels(hass):
    # PostNL leaves sourceDisplayName null unless the parcel is shared; the
    # raw field must not be back-filled with the title (#13).
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {"3SOWN": {"statusPhase": {"message": "Pakket is onderweg"}}}
    })
    shipment = {
        "key": "K14",
        "barcode": "3SOWN",
        "title": "Shop Two",
        "sourceAccountId": None,
        "sourceDisplayName": None,
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["sender"] == "Shop Two"
    assert parcel["raw"]["source_display_name"] is None


async def test_transform_shipment_fetches_planned_window_from_route_information(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SABC": {
                "statusPhase": {"message": "OP_WEG_VAN_AFZENDER"},
                "routeInformation": {
                    "plannedDeliveryTime": "2026-06-17T15:00:00Z",
                    "plannedDeliveryTimeWindow": {
                        "startDateTime": "2026-06-17T14:00:00Z",
                        "endDateTime": "2026-06-17T16:00:00Z",
                    },
                    "expectedDeliveryTime": "2026-06-17T15:15:00Z",
                },
            }
        }
    })
    shipment = {
        "key": "K2",
        "barcode": "3SABC",
        "title": "Brand",
        "detailsUrl": None,
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "OP_WEG_VAN_AFZENDER"
    assert parcel["planned_from"] == "2026-06-17T14:00:00Z"
    assert parcel["planned_to"] == "2026-06-17T16:00:00Z"
    assert parcel["raw"]["expected_datetime"] == "2026-06-17T15:15:00Z"


async def test_transform_shipment_falls_back_to_eta_window(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "BEZIG_MET_BEZORGEN"},
                "eta": {
                    "start": "2026-06-17T11:00:00Z",
                    "end": "2026-06-17T13:00:00Z",
                },
            }
        }
    })
    shipment = {
        "key": "K3",
        "barcode": "3SXYZ",
        "title": "Brand",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "BEZIG_MET_BEZORGEN"
    assert parcel["planned_from"] == "2026-06-17T11:00:00Z"
    assert parcel["planned_to"] == "2026-06-17T13:00:00Z"


async def test_transform_shipment_falls_back_to_delivery_window_strings(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SDEF": {
                "statusPhase": {"message": "VERWACHT"},
                # No routeInformation, no eta
            }
        }
    })
    shipment = {
        "key": "K4",
        "barcode": "3SDEF",
        "title": "Brand",
        "delivered": False,
        "deliveryWindowFrom": "2026-06-18T09:00:00Z",
        "deliveryWindowTo": "2026-06-18T17:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["planned_from"] == "2026-06-18T09:00:00Z"
    assert parcel["planned_to"] == "2026-06-18T17:00:00Z"


async def test_transform_shipment_receiver_from_recipient_person_name(hass):
    """Active path picks up recipient name from colli.recipient.names.personName."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
                "recipient": {"names": {"personName": "Peter Nijssen"}},
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "receiverTitle": "Fallback",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Peter Nijssen"


async def test_transform_shipment_receiver_falls_back_to_receiver_title(hass):
    """When colli.recipient.personName is missing, fall back to GraphQL receiverTitle."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "receiverTitle": "Fallback Name",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Fallback Name"


async def test_transform_shipment_delivered_receiver_uses_receiver_title(hass):
    coordinator = _make_coordinator(hass)
    shipment = {
        "key": "K",
        "barcode": "3SDEL",
        "title": "Brand",
        "receiverTitle": "Peter ",
        "delivered": True,
        "deliveredTimeStamp": "2026-06-15T14:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Peter"
    # Delivered path skips Track & Trace, so no weight / dimensions available.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


async def test_transform_shipment_extracts_native_dimensions_from_colli(hass):
    """colli.details.dimensions surfaces as native g+mm on raw and converted on the top level."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
                "details": {
                    "dimensions": {
                        "weight": 1500, "depth": 300, "width": 200, "height": 150,
                    },
                },
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["weight"] == 1.5
    assert parcel["dimensions"]["text"] == "30 x 20 x 15 cm"
    assert parcel["raw"]["dimensions"]["weight"] == 1500
    assert parcel["raw"]["dimensions"]["depth"] == 300


# ---------------------------------------------------------------------------
# _fire_change_events
# ---------------------------------------------------------------------------


def _capture(hass, event_type: str) -> list:
    events: list = []
    hass.bus.async_listen(event_type, events.append)
    return events


def _norm(barcode: str, status_message: str, *, delivered: bool = False) -> dict:
    return normalize_parcel({
        "barcode": barcode,
        "delivered": delivered,
        "status_message": status_message,
    })


async def test_fire_change_events_silent_on_first_refresh(hass):
    coordinator = _make_coordinator(hass)
    reg = _capture(hass, "postnl_parcel_registered")
    chg = _capture(hass, "postnl_parcel_status_changed")

    # _known_state is None on a fresh coordinator → suppress.
    coordinator._fire_change_events([_norm("A", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert reg == []
    assert chg == []


async def test_fire_change_events_emits_registered_for_new_barcode(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_registered")

    coordinator._fire_change_events([
        _norm("A", "Pakket is onderweg"),
        _norm("NEW", "Pakket is aangemeld"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "NEW"
    assert payload["status"] == ParcelStatus.REGISTERED
    assert payload["carrier"] == "PostNL"


async def test_fire_change_events_emits_status_changed(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket wordt vandaag bezorgd")])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_status"] == ParcelStatus.IN_TRANSIT
    assert payload["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert payload["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_fire_change_events_delivered_takes_precedence(hass):
    """The hop to DELIVERED fires parcel_delivered and NOT status_changed."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.OUT_FOR_DELIVERY}
    delivered = _capture(hass, "postnl_parcel_delivered")
    changed = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([
        _norm("A", "Pakket is bezorgd", delivered=True),
    ])
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    payload = delivered[0].data
    assert payload["barcode"] == "A"
    assert payload["status"] == ParcelStatus.DELIVERED


async def test_fire_change_events_silent_for_new_delivered_barcode(hass):
    """A barcode first seen already delivered fires neither registered nor delivered."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    reg = _capture(hass, "postnl_parcel_registered")
    delivered = _capture(hass, "postnl_parcel_delivered")

    coordinator._fire_change_events([
        _norm("A", "Pakket is onderweg"),
        _norm("B", "Pakket is bezorgd", delivered=True),
    ])
    await hass.async_block_till_done()

    assert reg == []
    assert delivered == []


async def test_fire_change_events_no_event_when_status_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    reg = _capture(hass, "postnl_parcel_registered")
    chg = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert reg == []
    assert chg == []


async def test_fire_change_events_intra_in_transit_does_not_fire(hass):
    """Different Dutch strings mapping to the same canonical status fire nothing.

    "ontvangen" and "gesorteerd" both map to IN_TRANSIT — the raw_status
    changes but the normalised status does not, so no event is emitted.
    """
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket is gesorteerd in het sorteercentrum")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_change_events_skips_parcels_without_barcode(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {}
    captured = _capture(hass, "postnl_parcel_registered")

    coordinator._fire_change_events([_norm("", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert captured == []


# ---------------------------------------------------------------------------
# Event firing — parcel_delivery_time_changed
# ---------------------------------------------------------------------------


def _norm_with_window(
    barcode: str, planned_from: str | None, planned_to: str | None
) -> dict:
    return normalize_parcel({
        "barcode": barcode,
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "planned_from": planned_from,
        "planned_to": planned_to,
    })


async def test_fire_change_events_delivery_time_changed_when_window_appears(hass):
    """A barcode whose planned_from gains a value fires delivery_time_changed."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"A": (None, None)}
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_planned_from"] is None
    assert payload["new_planned_from"] == "2026-06-17T14:00:00Z"


async def test_fire_change_events_delivery_time_changed_when_window_shifts(hass):
    """A barcode whose planned_from changes to a different value fires the event."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T10:00:00Z", "2026-06-17T12:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0].data["old_planned_from"] == "2026-06-17T10:00:00Z"
    assert captured[0].data["new_planned_from"] == "2026-06-17T14:00:00Z"


async def test_fire_change_events_no_delivery_time_event_when_window_clears(hass):
    """value -> null transitions are intentionally silent."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T10:00:00Z", "2026-06-17T12:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([_norm_with_window("A", None, None)])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_change_events_no_delivery_time_event_when_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert captured == []


# ---------------------------------------------------------------------------
# _fire_outgoing_change_events
# ---------------------------------------------------------------------------


async def test_fire_outgoing_silent_on_first_refresh(hass):
    coordinator = _make_coordinator(hass)
    chg = _capture(hass, "postnl_outgoing_parcel_status_changed")
    dlv = _capture(hass, "postnl_outgoing_parcel_delivered")

    # _known_outgoing_state is None on a fresh coordinator → suppress.
    coordinator._fire_outgoing_change_events([_norm("S", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert chg == []
    assert dlv == []


async def test_fire_outgoing_emits_status_changed(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_outgoing_state = {"S": ParcelStatus.REGISTERED}
    captured = _capture(hass, "postnl_outgoing_parcel_status_changed")

    coordinator._fire_outgoing_change_events([_norm("S", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "S"
    assert payload["old_status"] == ParcelStatus.REGISTERED
    assert payload["new_status"] == ParcelStatus.IN_TRANSIT


async def test_fire_outgoing_delivered_takes_precedence(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_outgoing_state = {"S": ParcelStatus.IN_TRANSIT}
    dlv = _capture(hass, "postnl_outgoing_parcel_delivered")
    chg = _capture(hass, "postnl_outgoing_parcel_status_changed")

    coordinator._fire_outgoing_change_events([
        _norm("S", "Bezorgd", delivered=True),
    ])
    await hass.async_block_till_done()

    assert len(dlv) == 1
    assert dlv[0].data["barcode"] == "S"
    assert chg == []


async def test_fire_outgoing_no_event_when_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_outgoing_state = {"S": ParcelStatus.DELIVERED}
    dlv = _capture(hass, "postnl_outgoing_parcel_delivered")
    chg = _capture(hass, "postnl_outgoing_parcel_status_changed")

    coordinator._fire_outgoing_change_events([
        _norm("S", "Bezorgd", delivered=True),
    ])
    await hass.async_block_till_done()

    assert dlv == []
    assert chg == []


# ---------------------------------------------------------------------------
# _fire_letter_events
# ---------------------------------------------------------------------------


async def test_fire_letter_events_silent_on_first_refresh(hass):
    coordinator = _make_coordinator(hass)
    captured = _capture(hass, "postnl_letter_announced")

    # _known_letter_ids is None on a fresh coordinator → suppress.
    coordinator._fire_letter_events([letter_sample("ABC1"), letter_sample("ABC2")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_letter_events_emits_announced_for_new_id(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = {"ABC1"}
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([letter_sample("ABC1"), letter_sample("NEW", title="17 juni")])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["id"] == "NEW"
    assert payload["title"] == "17 juni"
    assert payload["image_url"] == "https://example.com/a.jpg"
    assert payload["carrier"] == "PostNL"


async def test_fire_letter_events_no_event_when_letter_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = {"ABC1"}
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([letter_sample("ABC1")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_letter_events_skips_letters_without_id(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = set()
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([letter_sample("")])
    await hass.async_block_till_done()

    assert captured == []


async def test_transform_shipment_handles_missing_colli_entry(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={"colli": {}})
    shipment = {
        "key": "K5",
        "barcode": "3SNOPE",
        "title": "Brand",
        "delivered": False,
        "deliveryWindowFrom": "2026-06-20T09:00:00Z",
        "deliveryWindowTo": "2026-06-20T17:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "Unknown"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["planned_from"] == "2026-06-20T09:00:00Z"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def _ts_parcel(barcode: str, planned_from: str | None = None, delivered_at: str | None = None) -> dict:
    return {
        "barcode": barcode,
        "planned_from": planned_from,
        "delivered_at": delivered_at,
    }


def test_sort_orders_ascending_by_planned_from():
    parcels = [
        _ts_parcel("late", planned_from="2026-06-15T10:00:00+00:00"),
        _ts_parcel("early", planned_from="2026-06-13T08:00:00+00:00"),
        _ts_parcel("mid", planned_from="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["early", "mid", "late"]


def test_sort_orders_descending_for_delivered_at():
    parcels = [
        _ts_parcel("oldest", delivered_at="2026-06-13T08:00:00+00:00"),
        _ts_parcel("newest", delivered_at="2026-06-15T10:00:00+00:00"),
        _ts_parcel("mid", delivered_at="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)]
    assert ordered == ["newest", "mid", "oldest"]


def test_sort_keeps_missing_or_garbage_timestamps_at_end():
    parcels = [
        _ts_parcel("no-ts"),
        _ts_parcel("garbage", planned_from="not-a-date"),
        _ts_parcel("early", planned_from="2026-06-13T08:00:00+00:00"),
        _ts_parcel("late", planned_from="2026-06-15T10:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered[:2] == ["early", "late"]
    assert set(ordered[2:]) == {"no-ts", "garbage"}


def test_sort_handles_z_suffix_timestamps():
    parcels = [
        _ts_parcel("a", planned_from="2026-06-15T10:00:00Z"),
        _ts_parcel("b", planned_from="2026-06-13T10:00:00Z"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["b", "a"]


def test_sort_mixes_naive_and_aware_timestamps_without_crashing():
    # Regression: PostNL sometimes returns mixed-tz timestamps in the same
    # bucket. The sort treated naive values as UTC, otherwise Python raises
    # "can't compare offset-naive and offset-aware datetimes".
    parcels = [
        _ts_parcel("aware", planned_from="2026-06-15T10:00:00+00:00"),
        _ts_parcel("naive", planned_from="2026-06-13T10:00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["naive", "aware"]


def test_sort_empty_input_returns_empty_list():
    assert sort_parcels_by_ts([], "planned_from") == []


# ---------------------------------------------------------------------------
# map_observation_status
# ---------------------------------------------------------------------------


def test_map_observation_status_known_codes():
    assert map_observation_status("A01") == ParcelStatus.REGISTERED
    assert map_observation_status("B01") == ParcelStatus.IN_TRANSIT
    assert map_observation_status("J01") == ParcelStatus.IN_TRANSIT
    assert map_observation_status("J05") == ParcelStatus.OUT_FOR_DELIVERY
    assert map_observation_status("J02") == ParcelStatus.AT_PICKUP_POINT
    assert map_observation_status("J12") == ParcelStatus.AT_PICKUP_POINT


def test_map_observation_status_live_catalogue_2026_06_29():
    """Milestone codes gathered from a live account on 2026-06-29."""
    assert map_observation_status("M02") == ParcelStatus.REGISTERED
    # Sorting / route handling + genuine delays → in_transit
    for code in ("J04", "J21", "J31", "J32", "J40", "J44", "G01"):
        assert map_observation_status(code) == ParcelStatus.IN_TRANSIT, code
    # Pickup point
    assert map_observation_status("I08") == ParcelStatus.AT_PICKUP_POINT
    # Delivered / collected
    assert map_observation_status("I01") == ParcelStatus.DELIVERED
    assert map_observation_status("I02") == ParcelStatus.DELIVERED
    assert map_observation_status("I05") == ParcelStatus.DELIVERED
    assert map_observation_status("A80") == ParcelStatus.DELIVERED  # signature = proof of delivery


def test_map_observation_status_live_catalogue_2026_07_10():
    """Milestone codes gathered from a live account on 2026-07-10."""
    assert map_observation_status("A03") == ParcelStatus.REGISTERED   # zending aangemeld
    assert map_observation_status("R01") == ParcelStatus.IN_TRANSIT   # zending is gesorteerd


def test_map_observation_status_live_catalogue_2026_07_18():
    """Milestone codes reported by users (issue #11)."""
    # Letterbox delivery, variant of I05 — was reported as 'unknown'.
    assert map_observation_status("I11") == ParcelStatus.DELIVERED


def test_map_observation_status_meta_codes_are_silent_null(caplog):
    """Notification/admin codes are known → no movement status, no warning."""
    for code in ("A04", "A18", "A19", "A25", "A65", "A94", "A95", "A96", "A98", "K33", "K50"):
        assert map_observation_status(code, "some text") is None, code
    assert "issues/new" not in caplog.text  # meta codes must not warn


def test_build_history_meta_events_carry_forward_the_stage():
    """Notification/admin events inherit the last milestone's stage rather than
    bouncing the timeline backwards (the ETA-after-out_for_delivery case)."""
    observations = [
        {"observationDate": "2026-05-29T08:00:00+02:00", "observationCode": "A96", "description": "Bezorging wijzigen mogelijk"},
        {"observationDate": "2026-05-29T09:00:00+02:00", "observationCode": "A01", "description": "nog niet ontvangen"},
        {"observationDate": "2026-05-29T10:00:00+02:00", "observationCode": "B01", "description": "ontvangen door PostNL"},
        {"observationDate": "2026-05-29T10:30:00+02:00", "observationCode": "A18", "description": "ETA initieel bepaald"},
        {"observationDate": "2026-05-29T12:15:42+02:00", "observationCode": "J05", "description": "Bezorger is onderweg"},
        {"observationDate": "2026-05-29T12:15:43+02:00", "observationCode": "A19", "description": "ETA gewijzigd"},
        {"observationDate": "2026-05-29T14:34:26+02:00", "observationCode": "I01", "description": "Pakket is bezorgd"},
    ]
    statuses = [e["status"] for e in build_history(observations)]
    assert statuses == [
        ParcelStatus.REGISTERED,         # A96 before any milestone → registered baseline
        ParcelStatus.REGISTERED,         # A01
        ParcelStatus.IN_TRANSIT,         # B01
        ParcelStatus.IN_TRANSIT,         # A18 carries in_transit (just after receipt)
        ParcelStatus.OUT_FOR_DELIVERY,   # J05
        ParcelStatus.OUT_FOR_DELIVERY,   # A19 carries out_for_delivery (not a step back)
        ParcelStatus.DELIVERED,          # I01
    ]


def test_build_history_leading_meta_uses_registered_baseline():
    """A meta event that lands before any milestone reads the registered
    baseline (a tracked parcel is at least pre-announced), not null."""
    observations = [
        {"observationDate": "2026-05-10T21:06:41+02:00", "observationCode": "A98",
         "description": "Voorgemelde zending verrijkt door PostNL regie."},
    ]
    assert build_history(observations)[0]["status"] == ParcelStatus.REGISTERED


def test_build_history_unmapped_code_does_not_carry_forward():
    """An unknown code stays null (we don't know it) even after a milestone."""
    observations = [
        {"observationDate": "2026-05-29T10:00:00+02:00", "observationCode": "B01", "description": "ontvangen"},
        {"observationDate": "2026-05-29T11:00:00+02:00", "observationCode": "ZZ8", "description": "iets nieuws"},
    ]
    statuses = [e["status"] for e in build_history(observations)]
    assert statuses == [ParcelStatus.IN_TRANSIT, None]


def test_map_observation_status_none_for_missing_code():
    assert map_observation_status(None) is None
    assert map_observation_status("") is None


def test_map_observation_status_none_for_unmapped_code(caplog):
    # A distinct code so the one-shot dedupe set does not hide the warning.
    assert map_observation_status("ZZ9", "Verstuurd via warpdrive") is None
    assert "ZZ9" in caplog.text
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# _extract_observations
# ---------------------------------------------------------------------------


def test_extract_observations_prefers_all_observations():
    colli = {
        "analyticsInfo": {"allObservations": [{"observationCode": "A01"}]},
        "observations": [{"observationCode": "J05"}],
    }
    assert _extract_observations(colli) == [{"observationCode": "A01"}]


def test_extract_observations_falls_back_to_observations():
    colli = {"observations": [{"observationCode": "J05"}]}
    assert _extract_observations(colli) == [{"observationCode": "J05"}]


def test_extract_observations_empty_when_neither_present():
    assert _extract_observations({}) == []
    assert _extract_observations({"analyticsInfo": {}}) == []


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_entry_shape_and_order():
    history = build_history(observations())
    assert [e["status"] for e in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
    ]
    # raw_status mirrors the Dutch description; timestamp passed through.
    assert history[-1]["raw_status"] == "Bezorger is onderweg"
    assert history[-1]["timestamp"] == "2026-05-22T11:01:21+02:00"
    assert set(history[0]) == {"timestamp", "status", "raw_status"}


def test_build_history_sorts_unsorted_input_oldest_first():
    history = build_history(list(reversed(observations())))
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_build_history_caps_to_max_events():
    many = [
        {"observationDate": f"2026-05-{day:02d}T10:00:00+02:00", "observationCode": "J01", "description": "Zending is gesorteerd"}
        for day in range(1, 26)
    ]
    history = build_history(many)
    assert len(history) == 20
    # Keeps the most recent 20 — oldest five are dropped.
    assert history[0]["timestamp"] == "2026-05-06T10:00:00+02:00"


def test_build_history_respects_custom_cap():
    assert len(build_history(observations(), max_events=2)) == 2


def test_build_history_unmapped_code_is_null_status():
    history = build_history([
        {"observationDate": "2026-05-22T11:01:21+02:00", "observationCode": "QQ1", "description": "Onbekend"},
    ])
    assert history[0]["status"] is None
    assert history[0]["raw_status"] == "Onbekend"


def test_build_history_skips_entries_without_timestamp():
    history = build_history([
        {"observationCode": "J05", "description": "no date"},
        {"observationDate": "2026-05-22T11:01:21+02:00", "observationCode": "J05", "description": "ok"},
    ])
    assert len(history) == 1


def test_build_history_keeps_unparseable_timestamp_after_parseable():
    history = build_history([
        {"observationDate": "garbage", "observationCode": "J05", "description": "bad"},
        {"observationDate": "2026-05-22T11:01:21+02:00", "observationCode": "J01", "description": "good"},
    ])
    assert history[0]["raw_status"] == "good"
    assert history[-1]["raw_status"] == "bad"


def test_build_history_empty_for_no_observations():
    assert build_history(None) == []
    assert build_history([]) == []


# ---------------------------------------------------------------------------
# normalize_parcel — history field
# ---------------------------------------------------------------------------


def test_normalize_parcel_history_defaults_to_none():
    parcel = normalize_parcel({
        "barcode": "X", "delivered": False, "status_message": "Pakket is onderweg",
    })
    assert parcel["history"] is None


def test_normalize_parcel_history_passes_through_top_level():
    events = [{"timestamp": "2026-05-22T11:01:21+02:00", "status": "out_for_delivery", "raw_status": "Bezorger is onderweg"}]
    parcel = normalize_parcel(
        {"barcode": "X", "delivered": False, "status_message": "Pakket is onderweg"},
        history=events,
    )
    assert parcel["history"] == events
    # Top-level, so it survives the aggregator's strip_raw(); not duplicated under raw.
    assert "history" not in parcel["raw"]


# ---------------------------------------------------------------------------
# transform_shipment — history wiring
# ---------------------------------------------------------------------------


def _make_history_coordinator(hass, *, include_history: bool):
    entry = MagicMock()
    entry.options = {CONF_INCLUDE_HISTORY: include_history}
    coordinator = PostNLCoordinator(hass, entry)
    coordinator.jouw_api = MagicMock()
    return coordinator


async def test_transform_shipment_builds_history_when_option_on(hass):
    coordinator = _make_history_coordinator(hass, include_history=True)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value=track_and_trace())
    shipment = {"key": "K", "barcode": "3SABC", "title": "Brand", "delivered": False}
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["history"] is not None
    assert parcel["history"][-1]["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_transform_shipment_no_history_when_option_off(hass):
    coordinator = _make_history_coordinator(hass, include_history=False)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value=track_and_trace())
    shipment = {"key": "K", "barcode": "3SABC", "title": "Brand", "delivered": False}
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["history"] is None


async def test_transform_shipment_delivered_fetches_history_when_option_on(hass):
    coordinator = _make_history_coordinator(hass, include_history=True)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value=track_and_trace())
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": True,
        "deliveredTimeStamp": "2026-05-22T12:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["delivered"] is True
    # Opt-in: delivered parcels DO call Track & Trace to get a timeline.
    coordinator.jouw_api.track_and_trace.assert_called_once_with("K")
    assert parcel["history"][-1]["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_transform_shipment_delivered_no_history_when_option_off(hass):
    coordinator = _make_history_coordinator(hass, include_history=False)
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": True,
        "deliveredTimeStamp": "2026-05-22T12:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["history"] is None
    # Default path must not make the extra call.
    coordinator.jouw_api.track_and_trace.assert_not_called()


async def test_transform_shipment_delivered_history_failure_is_non_fatal(hass):
    import requests

    coordinator = _make_history_coordinator(hass, include_history=True)
    coordinator.jouw_api.track_and_trace = MagicMock(
        side_effect=requests.exceptions.RequestException("boom")
    )
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": True,
        "deliveredTimeStamp": "2026-05-22T12:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    # A failed history fetch must not break the delivered parcel.
    assert parcel["delivered"] is True
    assert parcel["history"] is None


# ---------------------------------------------------------------------------
# _device_id — resolved from the device registry, cached, attached to events
# ---------------------------------------------------------------------------


async def test_device_id_resolves_and_caches(hass):
    """_device_id finds the account's device and caches it for later events."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.postnl.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={},
        options={},
    )
    entry.add_to_hass(hass)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "abc-123")},
    )

    coordinator = PostNLCoordinator(hass, entry)

    assert coordinator._device_id() == device.id
    # Second call returns the cached value (no second registry lookup).
    assert coordinator._device_id() == device.id


async def test_device_id_none_when_no_device(hass):
    """_device_id stays None until a device has been registered."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.postnl.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="nobody@example.com",
        data={},
        options={},
    )
    entry.add_to_hass(hass)

    coordinator = PostNLCoordinator(hass, entry)
    assert coordinator._device_id() is None

async def test_transform_shipment_delivered_history_cached_per_barcode(hass):
    """A delivered parcel's history is fetched once, then served from cache."""
    coordinator = _make_history_coordinator(hass, include_history=True)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value=track_and_trace())
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": True,
        "deliveredTimeStamp": "2026-05-22T12:00:00Z",
    }
    first = await coordinator.transform_shipment(shipment)
    second = await coordinator.transform_shipment(shipment)
    coordinator.jouw_api.track_and_trace.assert_called_once_with("K")
    assert second["history"] == first["history"]


async def test_transform_shipment_delivered_history_failure_not_cached(hass):
    """A failed history fetch is retried on the next poll (not cached)."""
    import requests

    coordinator = _make_history_coordinator(hass, include_history=True)
    coordinator.jouw_api.track_and_trace = MagicMock(
        side_effect=[requests.exceptions.ConnectionError("boom"), track_and_trace()]
    )
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": True,
        "deliveredTimeStamp": "2026-05-22T12:00:00Z",
    }
    first = await coordinator.transform_shipment(shipment)
    assert first["history"] is None
    second = await coordinator.transform_shipment(shipment)
    assert second["history"] is not None
    assert coordinator.jouw_api.track_and_trace.call_count == 2


# ---------------------------------------------------------------------------
# transform_shipment — per-parcel degradation on Track & Trace failure
# ---------------------------------------------------------------------------


async def test_transform_shipment_reuses_cached_parcel_on_tt_failure(hass):
    """A transient T&T failure degrades to the previous transform for that
    parcel instead of failing the whole refresh."""
    import requests

    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SABC": {"statusPhase": {"message": "Onderweg"}}
        }
    })
    shipment = {"key": "K", "barcode": "3SABC", "title": "Brand", "delivered": False}
    good = await coordinator.transform_shipment(shipment)
    assert good["status"] == ParcelStatus.IN_TRANSIT

    coordinator.jouw_api.track_and_trace = MagicMock(
        side_effect=requests.exceptions.ConnectionError("boom")
    )
    degraded = await coordinator.transform_shipment(shipment)
    assert degraded == good


async def test_transform_shipment_falls_back_to_shipment_fields_on_tt_failure(hass):
    """Without a cached transform, a T&T failure still yields a parcel built
    from the GraphQL shipment fields rather than raising."""
    import requests

    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(
        side_effect=requests.exceptions.ConnectionError("boom")
    )
    shipment = {
        "key": "K", "barcode": "3SABC", "title": "Brand", "delivered": False,
        "deliveryWindowFrom": "2026-06-17T14:00:00Z",
        "deliveryWindowTo": "2026-06-17T16:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["barcode"] == "3SABC"
    assert parcel["planned_from"] == "2026-06-17T14:00:00Z"
    assert parcel["status"] == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# async_get_jouw_api — token-refresh-before-use for the on-demand image path
# ---------------------------------------------------------------------------


def _make_auth_coordinator(hass, *, access_token: str = "tok1"):
    entry = MagicMock()
    entry.options = {}
    auth = MagicMock()
    auth.check_and_refresh_token = AsyncMock()
    auth.access_token = access_token
    entry.runtime_data.auth = auth
    coordinator = PostNLCoordinator(hass, entry)
    return coordinator, auth


async def test_async_get_jouw_api_refreshes_token_before_returning(hass):
    coordinator, auth = _make_auth_coordinator(hass)
    await coordinator.async_get_jouw_api()
    auth.check_and_refresh_token.assert_awaited_once()


async def test_async_get_jouw_api_builds_client_on_first_call(hass):
    coordinator, _ = _make_auth_coordinator(hass)
    assert coordinator.jouw_api is None
    result = await coordinator.async_get_jouw_api()
    assert result is coordinator.jouw_api
    assert coordinator.jouw_api is not None
    assert coordinator.graphq_api is not None


async def test_async_get_jouw_api_reuses_client_when_token_unchanged(hass):
    # Regression guard for the "API clients are reused across polls" rule in
    # CLAUDE.md — a client each carries its own requests.Session connection
    # pool, so rebuilding on every on-demand image fetch would leak one per
    # fetch instead of reusing the poll cycle's.
    coordinator, _ = _make_auth_coordinator(hass)
    first = await coordinator.async_get_jouw_api()
    second = await coordinator.async_get_jouw_api()
    assert first is second


async def test_async_get_jouw_api_rebuilds_client_when_token_changed(hass):
    # The actual bug this guards against: PostNLLetterImage.async_image() used
    # to read coordinator.jouw_api directly, so a bearer token that expired
    # between polls (the default poll interval is 30 minutes; PostNL's own
    # access tokens are not guaranteed to live that long) produced a 401 on
    # every image fetch until the next poll happened to refresh it.
    coordinator, auth = _make_auth_coordinator(hass, access_token="tok1")
    first = await coordinator.async_get_jouw_api()
    auth.access_token = "tok2"
    second = await coordinator.async_get_jouw_api()
    assert first is not second
    assert coordinator._api_token == "tok2"


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_omit_pickup_point():
    """PostNL never names the actual service point — pickup_point stays None."""
    assert "pickup_point" not in CAPABILITIES

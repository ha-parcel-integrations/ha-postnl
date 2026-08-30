"""Constants for the postnl integration."""

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "postnl"

PLATFORMS = [
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.SENSOR,
    Platform.IMAGE,
]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. PostNL is one of the suite's most complete carriers —
# weight, dimensions, delivery window, url, and history are all populated;
# pickup_point is the one gap, hard-coded None even though "pickup"
# (delivery_address_type == "ServicePoint") is detected.
CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "url", "history"}
)

CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_AUTO = "auto"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30  # minutes — default for entries that predate "auto"
# New config entries default to "auto" (dynamic-polling rollout, 2026-08-30);
# an existing entry keeps whatever it already has, numeric or "auto".
DEFAULT_NEW_REFRESH_INTERVAL = REFRESH_INTERVAL_AUTO

# Dynamic, status-driven polling — selected via "auto" above. See
# carrier-research/dynamic-polling.md for the full algorithm and reasoning.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active
# receiver parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of its
# planned_from (or has no planned_from at all); mid = anything else still in
# flight. This is an account-based coordinator (Section 2.2), so it never
# fully stops — the mid-tier poll is also how a new shipment (or letter, via
# the same combined call) gets discovered.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False
# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20


class ParcelStatus(StrEnum):
    """ParcelStatus."""

    REGISTERED = "registered"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AT_PICKUP_POINT = "at_pickup_point"
    DELIVERED = "delivered"
    RETURNING = "returning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"

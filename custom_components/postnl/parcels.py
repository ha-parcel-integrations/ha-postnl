"""Pure parcel mapping, normalization and list helpers for PostNL.

No I/O and no Home Assistant objects beyond the config entry's options: this is
the carrier-specific status mapping and canonical-shape logic, kept apart from
the coordinator (fetching, caching, events) so it stays trivially unit-testable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)


# PostNL's status comes from three signals, tried in this order:
#   - shipment.delivered (bool, GraphQL) — terminal indicator, always wins
#   - colli.analyticsInfo.allObservations[].observationCode — stable vocabulary
#     (see _OBSERVATION_CODE_MAP below); preferred whenever present
#   - colli.statusPhase.message (Track & Trace) — Dutch human-readable string;
#     fallback for when observation data is absent or inconclusive
#
# statusPhase.message is not under an API contract: PostNL changes the wording
# at will (five separate "unrecognised status" issues have been the same root
# cause — a new phrasing slips past the pattern list). observationCode doesn't
# have that failure mode, which is why it now takes priority; the substring
# patterns remain as the fallback path. Order matters — first match wins, so
# the more specific patterns must come before the broader ones (e.g. "wordt
# vandaag bezorgd" before "bezorgd").
_STATUS_PATTERNS: tuple[tuple[str, ParcelStatus], ...] = (
    ("ligt klaar bij postnl punt", ParcelStatus.AT_PICKUP_POINT),
    ("afgeleverd op postnl punt", ParcelStatus.AT_PICKUP_POINT),
    ("klaar bij postnl punt", ParcelStatus.AT_PICKUP_POINT),
    ("teruggestuurd", ParcelStatus.RETURNING),
    ("retour", ParcelStatus.RETURNING),
    ("wordt vandaag bezorgd", ParcelStatus.OUT_FOR_DELIVERY),
    ("onderweg naar het bezorgadres", ParcelStatus.OUT_FOR_DELIVERY),
    ("onderweg naar de bezorger", ParcelStatus.OUT_FOR_DELIVERY),
    ("aangemeld", ParcelStatus.REGISTERED),
    ("verwacht", ParcelStatus.REGISTERED),
    ("bezorgmoment is bijgewerkt", ParcelStatus.IN_TRANSIT),
    ("lukt vandaag niet", ParcelStatus.IN_TRANSIT),
    ("duurt de bezorging wat langer", ParcelStatus.IN_TRANSIT),
    ("ontvangen", ParcelStatus.IN_TRANSIT),
    ("gesorteerd", ParcelStatus.IN_TRANSIT),
    ("onderweg", ParcelStatus.IN_TRANSIT),
    ("klaar voor verzending", ParcelStatus.IN_TRANSIT),
    ("je zending ligt op een sorteercentrum in land van bestemming", ParcelStatus.IN_TRANSIT),
    ("de grens over", ParcelStatus.IN_TRANSIT),
    ("aangekomen in het land van bestemming", ParcelStatus.IN_TRANSIT),
    ("je zending is bezorgd bij de ontvanger", ParcelStatus.DELIVERED),
    ("bezorgd", ParcelStatus.DELIVERED),
    ("unknown", ParcelStatus.UNKNOWN),
)

# History events carry a stable PostNL ``observationCode`` (e.g. ``J05``)
# rather than the brittle human text, so the timeline maps from the code.
# Unmapped codes resolve to ``None`` (+ a one-shot warning, see feature B).
# Milestone codes — events that represent a real movement stage. These drive
# the history ``status`` and progress monotonically (registered → in_transit →
# out_for_delivery → delivered), with the one legitimate exception of a real
# delivery delay/failure (G/T codes) dropping back to in_transit.
_OBSERVATION_CODE_MAP: dict[str, ParcelStatus] = {
    # --- Pre-receipt baseline (registered) ---
    "A01": ParcelStatus.REGISTERED,       # nog niet ontvangen/verwerkt
    "A03": ParcelStatus.REGISTERED,       # zending aangemeld (pre-advice)
    "M02": ParcelStatus.REGISTERED,       # nog niet ontvangen/verwerkt (variant of A01)
    # --- In the network (in_transit) ---
    "B01": ParcelStatus.IN_TRANSIT,       # ontvangen door PostNL
    "J01": ParcelStatus.IN_TRANSIT,       # gesorteerd
    "R01": ParcelStatus.IN_TRANSIT,       # zending is gesorteerd (variant of J01)
    "J04": ParcelStatus.IN_TRANSIT,       # voorgemeld en gescand op rit
    "J21": ParcelStatus.IN_TRANSIT,       # overgedragen aan afhaallocatie (handover, not yet ready)
    "J31": ParcelStatus.IN_TRANSIT,       # ingenomen aan de sorteergoot
    "J32": ParcelStatus.IN_TRANSIT,       # overgedragen tijdens route
    "J30": ParcelStatus.IN_TRANSIT,       # vze ingenomen door planbalie
    "J39": ParcelStatus.IN_TRANSIT,       # PostNL-punt was vol; onderweg naar een ander PostNL-punt
    "J40": ParcelStatus.IN_TRANSIT,       # voorgemeld en gesorteerd op rit
    "J44": ParcelStatus.IN_TRANSIT,       # overgenomen tijdens route
    "J55": ParcelStatus.IN_TRANSIT,       # verwacht bij PostNL-punt
    "X01": ParcelStatus.IN_TRANSIT,       # zending is klaar voor verzending naar land van bestemming
    "X02": ParcelStatus.IN_TRANSIT,       # yes! je zending is aangekomen in het land van bestemming
    "X03": ParcelStatus.IN_TRANSIT,       # pakket wordt ingeklaard in het land van bestemming (customs)
    "X04": ParcelStatus.IN_TRANSIT,       # vrijgegeven in het land van bestemming, doorgestuurd naar geadresseerde
    "X08": ParcelStatus.IN_TRANSIT,       # je zending ligt op een sorteercentrum in land van bestemming
    "X19": ParcelStatus.IN_TRANSIT,       # je zending is de grens over
    "A21": ParcelStatus.IN_TRANSIT,       # betaald, pakket wordt klaargemaakt voor bezorging (post-customs)
    "I07": ParcelStatus.IN_TRANSIT,       # retourzending gesorteerd, wordt overgedragen aan afzender
    # --- Real delivery delay / failed attempt: a genuine step back to transit ---
    "G01": ParcelStatus.IN_TRANSIT,       # bezorgmoment bijgewerkt — lukt vandaag niet
    "G05": ParcelStatus.IN_TRANSIT,       # bezorgmoment bijgewerkt (delay)
    "K70": ParcelStatus.IN_TRANSIT,       # bezorging niet gelukt, pakket gaat naar PostNL-punt
    "T04": ParcelStatus.IN_TRANSIT,       # bezorgmoment bijgewerkt (delay)
    # --- Out for delivery ---
    "J05": ParcelStatus.OUT_FOR_DELIVERY, # bezorger is onderweg
    # --- At a pickup point ---
    "I08": ParcelStatus.AT_PICKUP_POINT,  # zending beschikbaar op PostNL-punt
    "J02": ParcelStatus.AT_PICKUP_POINT,  # ligt klaar bij PostNL-punt
    "J12": ParcelStatus.AT_PICKUP_POINT,  # ligt klaar bij PostNL-punt
    "J23": ParcelStatus.AT_PICKUP_POINT,  # kan worden afgehaald bij de pakketautomaat (locker variant of J02/J12)
    # --- Delivered / collected (terminal) ---
    "A80": ParcelStatus.DELIVERED,        # handtekening voor pakket ontvangen (proof of delivery)
    "I01": ParcelStatus.DELIVERED,        # pakket is bezorgd
    "I02": ParcelStatus.DELIVERED,        # afgehaald bij PostNL-punt
    "I05": ParcelStatus.DELIVERED,        # bezorgd in de brievenbus
    "I11": ParcelStatus.DELIVERED,        # zending is bezorgd in de brievenbus (variant of I05)
    "Z01": ParcelStatus.DELIVERED,        # yes! Je zending is bezorgd bij de ontvanger
}

# Known notification / admin / data events that are NOT a movement milestone.
# PostNL interleaves these throughout the timeline (often out of order), so
# mapping them to a movement status makes the history bounce back and forth
# (e.g. an "ETA gewijzigd" right after "Bezorger is onderweg" would otherwise
# drag the status from out_for_delivery back to in_transit). They resolve to
# ``status: null`` — known, so no "unrecognised" warning — while the exact
# label is preserved on ``raw_status``.
_OBSERVATION_META_CODES: frozenset[str] = frozenset({
    "A04",  # PIOS melding (notification)
    "A18",  # ETA initieel bepaald
    "A19",  # ETA gewijzigd
    "A24",  # herinnering 24 uur in pakketautomaat (locker variant of A25)
    "A25",  # reminderservice notificaties
    "A65",  # zending herpland (planning change)
    "A94",  # zelf bezorging gewijzigd
    "A95",  # bezorging wijzigen niet mogelijk
    "A96",  # bezorging wijzigen mogelijk
    "A98",  # voorgemelde zending verrijkt door PostNL regie (data enrichment)
    "A20",  # verzoek tot betaling kosten voor zending verstuurd (customs invoice notice)
    "J09",  # bezorgmoment bijgewerkt, geen mislukte poging (locker parcel does not move)
    "K33",  # "leeg" placeholder
    "K50",  # RCS melding (notification)
})

# New-issue link surfaced in the unknown-status warnings so users can paste a
# ready-made line into a bug report. Points at the pre-filled issue template
# rather than a blank form, so a user following this link from their log lands
# somewhere that already asks the right questions.
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-postnl/issues/new"
    "?template=unrecognised_status.yml"
)

# One-shot dedupe sets so each distinct unmapped value warns once per HA
# session rather than on every poll.
_LOGGED_UNKNOWN_STATUSES: set[str] = set()
_LOGGED_UNKNOWN_OBSERVATION_CODES: set[str] = set()


def map_parcel_status(parcel: dict) -> ParcelStatus:
    """Map a PostNL parcel dict to a canonical :class:`ParcelStatus`.

    ``parcel`` is the intermediate dict built by :meth:`transform_shipment`
    with at least ``delivered`` (bool) and ``status_message`` (str) populated,
    plus the optional ``observations`` list (raw Track & Trace observations —
    present whenever the active-path T&T call returned a ``colli``,
    independent of the opt-in history option). The ``delivered`` flag from
    GraphQL is authoritative and short-circuits to :attr:`ParcelStatus.DELIVERED`.
    Next, :func:`derive_observation_status` is tried — the stable
    ``observationCode`` vocabulary, when it has anything to say. Only when
    that comes back empty (no observations, or none recognised) do we fall
    back to substring-matching ``status_message``. Anything that does not
    match any pattern is reported as :attr:`ParcelStatus.UNKNOWN` and
    surfaced once at info level so users can open an issue to extend the map.
    """
    if parcel.get("delivered"):
        return ParcelStatus.DELIVERED

    observation_status = derive_observation_status(parcel.get("observations"))
    if observation_status is not None:
        return observation_status

    # PostNL writes the pickup-point phrase with a hyphen ("PostNL-punt"),
    # not a space (#16) — normalise so the "postnl punt" patterns below
    # match regardless of which one PostNL uses in a given message.
    raw = (parcel.get("status_message") or "").strip().lower().replace("-", " ")
    if not raw:
        return ParcelStatus.UNKNOWN

    for pattern, status in _STATUS_PATTERNS:
        if pattern in raw:
            return status

    if raw not in _LOGGED_UNKNOWN_STATUSES:
        _LOGGED_UNKNOWN_STATUSES.add(raw)
        _LOGGER.warning(
            "Unrecognised PostNL status — help us map it. Open an issue and "
            "paste this line: %s\n  [parcel] statusPhase.message=%r "
            "→ reported as 'unknown'",
            _NEW_ISSUE_URL,
            parcel.get("status_message"),
        )
    return ParcelStatus.UNKNOWN


def map_observation_status(
    code: str | None, description: str | None = None
) -> ParcelStatus | None:
    """Map a Track & Trace ``observationCode`` to a canonical milestone status.

    Returns a :class:`ParcelStatus` only for genuine movement milestones.
    Known notification/admin codes (:data:`_OBSERVATION_META_CODES`) and
    unmapped codes both return ``None``; meta codes do so silently, while an
    unmapped code surfaces a one-shot warning with copy-paste issue text so
    users can help extend the map. :func:`build_history` decides what a
    ``None`` becomes (carry the previous stage forward for meta, leave null
    for unknown).
    """
    if not code:
        return None
    status = _OBSERVATION_CODE_MAP.get(code)
    if status is not None:
        return status

    if code in _OBSERVATION_META_CODES:
        return None  # known notification/admin event — no own movement status

    if code not in _LOGGED_UNKNOWN_OBSERVATION_CODES:
        _LOGGED_UNKNOWN_OBSERVATION_CODES.add(code)
        _LOGGER.warning(
            "Unrecognised PostNL status — help us map it. Open an issue and "
            "paste this line: %s\n  [history] observationCode=%s text=%r "
            "→ reported as 'unknown'",
            _NEW_ISSUE_URL,
            code,
            description,
        )
    return None


def _order_observations(observations: list[dict] | None) -> list[tuple]:
    """Sort raw observations oldest → newest into ``(timestamp, code, description)``.

    Entries with an unparseable timestamp keep their original order, appended
    after the parseable ones — shared by :func:`build_history` and
    :func:`derive_observation_status` so both walk the exact same order.
    """
    parseable: list[tuple[datetime, tuple]] = []
    unparseable: list[tuple] = []
    for obs in observations or []:
        timestamp = obs.get("observationDate")
        if not timestamp:
            continue
        record = (timestamp, obs.get("observationCode"), obs.get("description"))
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(record)
        else:
            parseable.append((dt, record))
    parseable.sort(key=lambda item: item[0])
    return [record for _, record in parseable] + unparseable


def _walk_milestones(
    ordered: list[tuple],
) -> tuple[list[tuple[str, ParcelStatus | None, str | None]], ParcelStatus]:
    """Walk ordered observations applying the milestone/meta carry-forward rule.

    Returns each record's derived status alongside the final stage reached —
    the shared core behind :func:`build_history` (needs every entry) and
    :func:`derive_observation_status` (needs only the final stage). See
    :func:`build_history` for what "carry-forward" means and why.
    """
    last_status: ParcelStatus = ParcelStatus.REGISTERED
    entries: list[tuple[str, ParcelStatus | None, str | None]] = []
    for timestamp, code, description in ordered:
        status = map_observation_status(code, description)
        if status is not None:
            last_status = status
        elif code in _OBSERVATION_META_CODES:
            status = last_status
        # else: unmapped → leave None (map_observation_status already warned)
        entries.append((timestamp, status, description))
    return entries, last_status


def derive_observation_status(observations: list[dict] | None) -> ParcelStatus | None:
    """Return the parcel's current stage derived from ``observationCode`` history.

    Applies the same milestone/meta carry-forward as :func:`build_history` —
    so a stray notification code (ETA recalc, ...) never becomes the answer —
    but returns only the final stage rather than a full timeline. This runs
    on every poll, independent of the opt-in :data:`CONF_INCLUDE_HISTORY`
    option: that option only gates whether the full timeline is *exposed* on
    ``history``, not whether the underlying observations are consulted.

    Returns ``None`` — signalling "fall back to text matching" — when there
    are no observations at all, or when every observation in the list is one
    we don't recognise (no milestone, no known meta code): defaulting to the
    ``REGISTERED`` baseline in that case would be a guess dressed as data.
    """
    ordered = _order_observations(observations)
    if not ordered:
        return None
    entries, last_status = _walk_milestones(ordered)
    if not any(status is not None for _, status, _ in entries):
        return None
    return last_status


def _extract_observations(colli: dict) -> list[dict]:
    """Return the status-event list from a colli object, oldest-first preferred.

    ``analyticsInfo.allObservations`` carries the **complete** timeline
    (oldest-first); the top-level ``observations`` list is truncated to the
    most recent few. Prefer the former, fall back to the latter.
    """
    analytics = colli.get("analyticsInfo") or {}
    return analytics.get("allObservations") or colli.get("observations") or []


def build_history(
    observations: list[dict] | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from raw Track & Trace observations.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. Sorted oldest → newest by timestamp (entries with an
    unparseable timestamp keep their original order, after the parseable ones)
    and capped to the most recent ``max_events``.

    PostNL interleaves notification/admin events (ETA recalcs, "bezorging
    wijzigen mogelijk", data enrichment, …) throughout the timeline, often out
    of order. Those carry **no movement status of their own**; instead they
    inherit the stage of the most recent milestone (carry-forward), so the
    timeline never bounces backwards on a cosmetic event — e.g. an "ETA
    gewijzigd" logged the moment the courier sets off reads ``out_for_delivery``,
    while the same code earlier (just after sorting) reads ``in_transit``.
    Genuine milestones drive the status and only a real delivery delay (G/T)
    steps back to ``in_transit``. Unmapped codes stay ``null`` (+ warning).

    A parcel that appears in track-trace has, at minimum, been pre-announced,
    so the implicit baseline stage is REGISTERED (:func:`_walk_milestones`'s
    starting point) — this is what a meta event (e.g. "Voorgemelde zending
    verrijkt") inherits when it lands before the first real milestone,
    instead of showing a bare null.
    """
    ordered = _order_observations(observations)
    entries, _ = _walk_milestones(ordered)
    history = [
        {"timestamp": timestamp, "status": status, "raw_status": description}
        for timestamp, status, description in entries
    ]
    return history[-max_events:]


def _convert_native_dimensions(
    native: dict | None,
) -> tuple[float | None, dict | None]:
    """Convert PostNL's native dimensions to the suite-wide canonical shape.

    The canonical shape is kg + cm with a pre-formatted ``text`` string.
    PostNL ships ``{height, width, depth, weight}`` in grams + millimetres,
    and calls the long edge ``depth`` rather than ``length``. The canonical
    contract every other carrier in the suite honours is kg + cm + ``length``,
    with a pre-formatted ``"L x W x H cm"`` string under ``text`` so dashboard
    cards can render it directly.
    """
    if not native:
        return None, None
    weight_g = native.get("weight")
    weight_kg = weight_g / 1000 if weight_g is not None else None
    depth_mm = native.get("depth")
    width_mm = native.get("width")
    height_mm = native.get("height")
    if depth_mm is None or width_mm is None or height_mm is None:
        return weight_kg, None
    length_cm = depth_mm / 10
    width_cm = width_mm / 10
    height_cm = height_mm / 10
    text = f"{int(round(length_cm))} x {int(round(width_cm))} x {int(round(height_cm))} cm"
    return weight_kg, {
        "length": length_cm,
        "width": width_cm,
        "height": height_cm,
        "text": text,
    }


def normalize_parcel(parcel: dict, *, history: list[dict] | None = None) -> dict:
    """Wrap a transformed PostNL parcel in the canonical carrier-agnostic shape.

    Top-level keys mirror DHL / DPD / parcel-aggregator. The original
    transformed PostNL payload (GraphQL + Track & Trace fields like
    ``status_message``, ``delivery_address_type``, ``planned_date``,
    ``expected_datetime`` and the native ``dimensions`` dict in g + mm)
    is preserved under ``raw`` for power users.

    ``history`` is the optional per-parcel status timeline (opt-in option,
    default off → ``None``). It stays top-level so it survives the
    aggregator's ``strip_raw()`` and flows through unchanged.
    """
    delivered = bool(parcel.get("delivered"))
    weight_kg, canonical_dimensions = _convert_native_dimensions(
        parcel.get("dimensions")
    )
    return {
        "carrier": "PostNL",
        "barcode": parcel.get("barcode"),
        # The GraphQL ``title`` (kept as ``name``) is the sender in every
        # observed payload. ``source_display_name`` is deliberately *not* the
        # first choice: for parcels visible through shared visibility in the
        # PostNL app it holds the sharing account holder's name rather than the
        # shop (#13). It stays as a last-resort fallback only.
        "sender": parcel.get("name") or parcel.get("source_display_name"),
        "receiver": parcel.get("receiver"),
        "status": map_parcel_status(parcel),
        "raw_status": parcel.get("status_message"),
        "delivered": delivered,
        "delivered_at": parcel.get("delivery_date") if delivered else None,
        "planned_from": None if delivered else parcel.get("planned_from"),
        "planned_to": None if delivered else parcel.get("planned_to"),
        "pickup": parcel.get("delivery_address_type") == "ServicePoint",
        "pickup_point": None,
        "url": parcel.get("url"),
        "weight": weight_kg,
        "dimensions": canonical_dimensions,
        "history": history,
        "raw": parcel,
    }


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    PostNL mixes offset-aware and naive timestamps in the same payload; naive
    values are treated as UTC so a mixed list still sorts without crashing.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _delivery_dt(parcel: dict) -> datetime | None:
    """Parse the delivery datetime from a normalised parcel dict."""
    return _parse_iso(parcel.get("delivered_at"))


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending`` — so freshly registered parcels without
    an ETA stay visible at the bottom instead of jumping to the top.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        value = parcel.get(key_field)
        if not isinstance(value, str) or not value:
            without_ts.append(parcel)
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            without_ts.append(parcel)
            continue
        # PostNL mixes offset-aware and naive timestamps in the same payload;
        # treat naive values as UTC so the sort doesn't crash on a mixed list.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


def _apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list according to the configured options."""
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    filter_amount = int(options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT))

    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=filter_amount)
        return [p for p in parcels if (dt := _delivery_dt(p)) is None or dt >= cutoff]

    return parcels[:filter_amount]

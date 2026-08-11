"""Data update coordinator for the postnl integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import AsyncConfigEntryAuth
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    ParcelStatus,
)
from .graphql import PostNLGraphql
from .jouw_api import PostNLJouwAPI
from .letters import extract_letters
from .parcels import (
    _apply_delivered_filter,
    _extract_observations,
    build_history,
    normalize_parcel,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval from the config entry options."""
    minutes = int(
        entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)
    )
    return timedelta(minutes=minutes)


class PostNLCoordinator(DataUpdateCoordinator):
    """Coordinator for the postnl integration."""

    data: dict[str, list[dict]]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize PostNL coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="PostNL",
            update_interval=_refresh_interval(entry),
        )
        self.delivered_receiver: list[dict] = []
        self.delivered_sender: list[dict] = []
        self.letters: list[dict] = []
        # API clients are reused across polls (each PostNLJouwAPI owns a
        # requests.Session with a connection pool); they are only rebuilt
        # when the access token actually changes.
        self.graphq_api: PostNLGraphql | None = None
        self.jouw_api: PostNLJouwAPI | None = None
        self._api_token: str | None = None
        # barcode -> last seen ParcelStatus. ``None`` on the first refresh so
        # we can suppress events for parcels that already existed when the
        # integration started (we do not know their previous state).
        self._known_state: dict[str, ParcelStatus] | None = None
        # barcode -> last seen (planned_from, planned_to) tuple. Mirrors
        # ``_known_state`` for delivery-time-change detection.
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # barcode -> last seen ParcelStatus for *outgoing* (sender) parcels —
        # own sent shipments and returns both land in senderShipments. Tracked
        # over the full sender list so a status change or a
        # transition-to-delivered fires an outgoing event. ``None`` on the
        # first refresh for the same suppression reason as ``_known_state``.
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        # Letter ids seen on the previous successful letters fetch. ``None``
        # on the first refresh for the same reason — we do not want to fire
        # ``postnl_letter_announced`` for every letter that already existed.
        self._known_letter_ids: set[str] | None = None
        # barcode -> last successfully transformed (normalized) parcel, so a
        # transient Track & Trace failure for one parcel degrades to its
        # previous data instead of failing the whole refresh.
        self._parcel_cache: dict[str, dict] = {}
        # barcode -> history timeline for *delivered* parcels. A delivered
        # parcel's history never changes, so the extra T&T call is made at
        # most once per barcode instead of on every poll.
        self._delivered_history_cache: dict[str, list[dict] | None] = {}
        # Cached device id for this account, attached to every fired event so
        # device-trigger automations can filter to a specific PostNL account.
        # ``None`` until the device exists (i.e. the sensors are set up).
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll, surfaced by a diagnostic
        # sensor so users can alert on a silently-stale integration (the
        # count sensors only change when a value changes, not every poll).
        self.last_success_time: datetime | None = None
        _LOGGER.debug("PostNLCoordinator initialized with update interval: %s", self.update_interval)

    def _device_id(self) -> str | None:
        """Resolve (and cache) this account's device id for event payloads.

        Looked up from the device registry by config entry. Stays ``None``
        until the device has been registered (the sensors create it on first
        setup), which is harmless because events are suppressed on the very
        first refresh anyway.
        """
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    async def async_get_jouw_api(self) -> PostNLJouwAPI:
        """Return ``jouw_api``, refreshing the bearer token first if it has expired.

        Every poll goes through ``_async_update_data`` below, which always
        refreshes before use — but ``PostNLLetterImage.async_image()`` calls this
        on demand, whenever a client actually requests the photo, independently of
        the poll cycle. ``jouw_api`` bakes its bearer token into the
        ``requests.Session`` once at construction (see its docstring) and is only
        rebuilt here when the token has actually changed, same as
        ``_async_update_data`` — so a poll shortly after an on-demand image fetch
        does not throw away a connection pool for nothing.
        """
        auth: AsyncConfigEntryAuth = self.config_entry.runtime_data.auth
        await auth.check_and_refresh_token()
        if self.jouw_api is None or auth.access_token != self._api_token:
            self._api_token = auth.access_token
            self.graphq_api = PostNLGraphql(self._api_token)
            self.jouw_api = PostNLJouwAPI(self._api_token)
        return self.jouw_api

    async def _async_update_data(self) -> dict[str, list[dict]]:
        _LOGGER.debug("Starting data update for PostNL.")
        try:
            _LOGGER.debug("Authenticating with PostNL API.")
            await self.async_get_jouw_api()

            data: dict[str, list[dict]] = {
                'receiver': [],
                'sender': []
            }

            shipments = await self.hass.async_add_executor_job(self.graphq_api.shipments)

            _LOGGER.debug("Shipments fetched: %s", shipments)
            receiver_shipments = [self.transform_shipment(shipment) for shipment in
                                  shipments.get('trackedShipments', {}).get('receiverShipments', [])]
            data['receiver'] = await asyncio.gather(*receiver_shipments)

            sender_shipments = [self.transform_shipment(shipment) for shipment in
                                shipments.get('trackedShipments', {}).get('senderShipments', [])]
            data['sender'] = await asyncio.gather(*sender_shipments)

            data['receiver'] = sort_parcels_by_ts(data['receiver'], 'planned_from')
            data['sender'] = sort_parcels_by_ts(data['sender'], 'planned_from')

            # Keep the fallback/history caches bounded to barcodes PostNL
            # still reports; anything older can never be needed again.
            current_barcodes = {
                p.get('barcode') for p in data['receiver'] if p.get('barcode')
            }
            self._parcel_cache = {
                k: v for k, v in self._parcel_cache.items() if k in current_barcodes
            }
            self._delivered_history_cache = {
                k: v
                for k, v in self._delivered_history_cache.items()
                if k in current_barcodes
            }

            # Incoming events over the full receiver list (active + delivered),
            # so the transition to delivered is visible in one set — mirrors
            # the outgoing events below.
            self._fire_change_events(data['receiver'])
            self._known_state = {
                p["barcode"]: p["status"]
                for p in data['receiver']
                if p.get("barcode")
            }
            self._known_delivery_times = {
                p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
                for p in data['receiver']
                if p.get("barcode")
            }

            delivered_receiver = [p for p in data['receiver'] if p.get('delivered')]
            self.delivered_receiver = sort_parcels_by_ts(
                self._apply_delivered_filter(delivered_receiver),
                'delivered_at',
                descending=True,
            )

            # Outgoing events over the full sender list (active + delivered),
            # so a hop from in-transit to delivered is visible in one set.
            self._fire_outgoing_change_events(data['sender'])
            self._known_outgoing_state = {
                p["barcode"]: p["status"]
                for p in data['sender']
                if p.get("barcode")
            }

            delivered_sender = [p for p in data['sender'] if p.get('delivered')]
            self.delivered_sender = sort_parcels_by_ts(
                self._apply_delivered_filter(delivered_sender),
                'delivered_at',
                descending=True,
            )

            try:
                letters_payload = await self.hass.async_add_executor_job(self.jouw_api.letters)
                self.letters = extract_letters(letters_payload)
                self._fire_letter_events(self.letters)
                self._known_letter_ids = {
                    letter["id"] for letter in self.letters if letter.get("id")
                }
            except requests.exceptions.RequestException as exception:
                _LOGGER.warning("PostNL letters fetch failed: %s", exception)

            _LOGGER.info(
                "Updated PostNL data: %d receiver packages, %d sender packages, %d delivered shown, %d letters.",
                len(data['receiver']), len(data['sender']), len(self.delivered_receiver), len(self.letters),
            )

            self.last_success_time = datetime.now(timezone.utc)
            return data
        except ConfigEntryAuthFailed:
            raise
        except HomeAssistantError as exception:
            raise UpdateFailed("Authentication failed") from exception
        except requests.exceptions.RequestException as exception:
            raise UpdateFailed("Unable to update PostNL data") from exception

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire events for newly-registered parcels and parcel transitions.

        Silent on the very first refresh — we cannot reliably know which
        parcels are "new" to the user vs. "already there before HA started".
        From the second refresh onward, every not-yet-delivered parcel that
        was not present before yields one ``postnl_parcel_registered`` event,
        every parcel whose normalised status transitions **to** ``DELIVERED``
        yields one ``postnl_parcel_delivered`` event, every other status
        change yields one ``postnl_parcel_status_changed`` event, and every
        parcel whose ``planned_from`` or ``planned_to`` changed to a non-null
        value yields one ``postnl_parcel_delivery_time_changed`` event.
        ``delivered`` takes precedence over ``status_changed`` for the
        terminal hop, and a parcel first seen already-delivered fires
        nothing — mirroring the outgoing events.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            # Fire only when at least one of the two ends up with a real
            # (non-null) value AND that value differs from the last-known
            # one. value -> null transitions are intentionally silent —
            # they mean the carrier dropped the ETA, which is not what
            # users want to be paged about.
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire status/delivered events for outgoing (sender) parcels.

        Silent on the very first refresh (``_known_outgoing_state is None``),
        matching ``_fire_change_events``. From the second refresh onward, every
        outgoing parcel whose normalised status transitions **to**
        ``DELIVERED`` yields one ``postnl_outgoing_parcel_delivered`` event, and
        every other status change yields one
        ``postnl_outgoing_parcel_status_changed`` event. ``delivered`` takes
        precedence over ``status_changed`` for that final transition, so the
        terminal hop fires exactly one (dedicated) event, not both. A parcel
        that is already delivered the first time it is seen never fires (its
        status did not change). There is no outgoing ``registered`` or
        ``delivery_time_changed`` event — those are intentionally out of scope.
        """
        if self._known_outgoing_state is None:
            return

        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            old_status = self._known_outgoing_state[barcode]
            new_status = parcel["status"]
            if new_status == old_status:
                continue

            if new_status == ParcelStatus.DELIVERED:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                )

    def _fire_letter_events(self, letters: list[dict]) -> None:
        """Fire ``postnl_letter_announced`` for newly-seen letter ids.

        Silent on the very first refresh — we cannot tell which letters
        are "new" vs "already announced before HA started". From the
        second refresh onward, every letter whose id was not present in
        the previous successful fetch yields one event with the full
        letter payload plus ``carrier: "PostNL"``.
        """
        if self._known_letter_ids is None:
            return
        device_id = self._device_id()
        for letter in letters:
            letter_id = letter.get("id")
            if not letter_id or letter_id in self._known_letter_ids:
                continue
            self.hass.bus.async_fire(
                f"{DOMAIN}_letter_announced",
                {**letter, "carrier": "PostNL", "device_id": device_id},
            )

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered receiver list according to the configured options."""
        return _apply_delivered_filter(parcels, self.config_entry)

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _delivered_history(self, shipment) -> list[dict] | None:
        """Fetch a history timeline for a delivered shipment (opt-in only).

        The active path gets observations from the Track & Trace call it
        already makes, but delivered parcels normally skip that call. When the
        history option is on we make the extra call here so delivered parcels
        get parity with DHL / DPD. A delivered parcel's history never changes,
        so a successful fetch is cached per barcode — one call per parcel
        ever, not one per poll. A failure is non-fatal — history is a
        nice-to-have, so we log and fall back to ``None`` (uncached, so the
        next poll retries) rather than failing the whole refresh.
        """
        if not self._include_history:
            return None
        barcode = shipment.get('barcode')
        if barcode and barcode in self._delivered_history_cache:
            return self._delivered_history_cache[barcode]
        try:
            details = self.jouw_api.track_and_trace(shipment['key'])
        except requests.exceptions.RequestException as exception:
            _LOGGER.warning(
                "History fetch failed for delivered shipment %s: %s",
                shipment.get('key'), exception,
            )
            return None
        colli = details.get('colli', {}).get(shipment['barcode'], {})
        history = build_history(_extract_observations(colli)) if colli else None
        if barcode:
            self._delivered_history_cache[barcode] = history
        return history

    async def transform_shipment(self, shipment) -> dict:
        """Transform a raw shipment into the canonical parcel shape."""
        _LOGGER.debug('Updating %s', shipment.get('key'))

        receiver_title = (shipment.get('receiverTitle') or '').strip() or None
        # ``title`` may arrive with a leading space, like ``receiverTitle``.
        title = (shipment.get('title') or '').strip() or None
        # ``sourceDisplayName`` is *not* the sender: it is the display name of
        # the account a shipment is shared from (paired with
        # ``sourceAccountId``). It is null for your own parcels and holds the
        # housemate's name for parcels you only see through shared visibility
        # in the PostNL app (#13). Kept raw so that signal survives.
        source_display_name = (shipment.get('sourceDisplayName') or '').strip() or None

        try:
            if shipment.get('delivered'):
                _LOGGER.debug('%s already delivered, no need to call jouw.postnl.', shipment.get('key'))

                # Delivered short-circuits the Track & Trace call, so there's no
                # ``colli.recipient.names.personName`` to read — the GraphQL
                # ``receiverTitle`` is the equivalent. No weight/dimensions
                # available on this path; the suite contract accepts those as
                # ``None``. History is the one exception: when the opt-in
                # option is on we make an extra T&T call for the timeline.
                history = await self.hass.async_add_executor_job(
                    self._delivered_history, shipment
                )
                return normalize_parcel({
                    "key": shipment.get('key'),
                    "barcode": shipment.get('barcode'),
                    "name": title,
                    "url": shipment.get('detailsUrl'),
                    "shipment_type": shipment.get('shipmentType'),
                    "receiver_title": receiver_title,
                    "receiver": receiver_title,
                    "source_display_name": source_display_name,
                    "status_message": "Pakket is bezorgd",
                    "delivered": shipment.get('delivered'),
                    "delivery_date": shipment.get('deliveredTimeStamp'),
                    "delivery_address_type": shipment.get('deliveryAddressType'),
                    "planned_date": None,
                    "planned_from": None,
                    "planned_to": None,
                    "expected_datetime": None,
                    "dimensions": None,
                }, history=history)

            _LOGGER.debug("Fetching Track and Trace details for shipment %s.", shipment['key'])
            try:
                track_and_trace_details = await self.hass.async_add_executor_job(
                    self.jouw_api.track_and_trace, shipment['key']
                )
            except requests.exceptions.RequestException as exception:
                # One broken T&T call must not fail the whole refresh. Degrade
                # per parcel: reuse the last successful transform when we have
                # one, otherwise fall through with the GraphQL-only fields.
                barcode = shipment.get('barcode')
                cached = self._parcel_cache.get(barcode) if barcode else None
                if cached is not None:
                    _LOGGER.warning(
                        "Track & Trace failed for shipment %s; reusing previous data: %s",
                        shipment.get('key'), exception,
                    )
                    return cached
                _LOGGER.warning(
                    "Track & Trace failed for shipment %s; using shipment fields only: %s",
                    shipment.get('key'), exception,
                )
                track_and_trace_details = {}

            if not track_and_trace_details.get('colli'):
                _LOGGER.debug("No colli found for shipment %s.", shipment['key'])
                _LOGGER.debug("Track and Trace response: %s", track_and_trace_details)

            colli = track_and_trace_details.get('colli', {}).get(shipment['barcode'], {})

            status_message = "Unknown"
            planned_date = planned_from = planned_to = expected_datetime = None
            recipient_name: str | None = None
            native_dimensions: dict | None = None
            history: list[dict] | None = None

            if colli:
                _LOGGER.debug("Colli details found for shipment %s: %s", shipment['key'], colli)
                if colli.get("routeInformation"):
                    route_information = colli.get("routeInformation")
                    planned_date = route_information.get("plannedDeliveryTime")
                    planned_from = route_information.get("plannedDeliveryTimeWindow", {}).get("startDateTime")
                    planned_to = route_information.get("plannedDeliveryTimeWindow", {}).get('endDateTime')
                    expected_datetime = route_information.get('expectedDeliveryTime')
                elif colli.get('eta'):
                    planned_date = colli.get('eta', {}).get('start')
                    planned_from = colli.get('eta', {}).get('start')
                    planned_to = colli.get('eta', {}).get('end')
                else:
                    planned_date = shipment.get('deliveryWindowFrom')
                    planned_from = shipment.get('deliveryWindowFrom')
                    planned_to = shipment.get('deliveryWindowTo')

                status_message = colli.get('statusPhase', {}).get('message', "Unknown")
                recipient_name = (
                    colli.get('recipient', {}).get('names', {}).get('personName')
                )
                native_dimensions = (colli.get('details') or {}).get('dimensions')
                # The active T&T call already carries the timeline; only build
                # it when the opt-in option is on.
                history = (
                    build_history(_extract_observations(colli))
                    if self._include_history
                    else None
                )
            else:
                _LOGGER.debug("Barcode not found in colli details for shipment %s.", shipment['key'])
                planned_date = shipment.get('deliveryWindowFrom')
                planned_from = shipment.get('deliveryWindowFrom')
                planned_to = shipment.get('deliveryWindowTo')

            parcel = normalize_parcel({
                "key": shipment.get('key'),
                "barcode": shipment.get('barcode'),
                "name": title,
                "url": shipment.get('detailsUrl'),
                "shipment_type": shipment.get('shipmentType'),
                "receiver_title": receiver_title,
                # ``recipient.personName`` from Track & Trace is the
                # authoritative recipient name on the active path;
                # ``receiverTitle`` is the GraphQL fallback for when T&T
                # doesn't have it yet (first poll after registration).
                "receiver": recipient_name or receiver_title,
                "source_display_name": source_display_name,
                "status_message": status_message,
                "delivered": shipment.get('delivered'),
                "delivery_date": shipment.get('deliveredTimeStamp'),
                "delivery_address_type": shipment.get('deliveryAddressType'),
                "planned_date": planned_date,
                "planned_from": planned_from,
                "planned_to": planned_to,
                "expected_datetime": expected_datetime,
                # Native PostNL dimensions (g + mm). Lives on the
                # intermediate dict so it surfaces under ``raw`` for power
                # users; ``normalize_parcel`` reads it and produces the
                # canonical kg + cm + ``text`` shape at the top level.
                "dimensions": native_dimensions,
            }, history=history)
            barcode = shipment.get('barcode')
            if barcode and colli:
                # Only a transform backed by real T&T data is worth reusing
                # as a fallback on a later transient failure.
                self._parcel_cache[barcode] = parcel
            return parcel
        except requests.exceptions.RequestException as exception:
            # Safety net for requests errors outside the targeted T&T catch.
            # Degrade to the last successful transform when we have one; only
            # fail the refresh when there is nothing to show for this parcel.
            _LOGGER.error("Error fetching Track and Trace details for shipment %s: %s", shipment.get('key'), exception, exc_info=True)
            barcode = shipment.get('barcode')
            cached = self._parcel_cache.get(barcode) if barcode else None
            if cached is not None:
                return cached
            raise UpdateFailed("Unable to update PostNL data") from exception

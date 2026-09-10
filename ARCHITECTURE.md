# Architecture

How `ha-postnl` is built and why it is built that way. `CLAUDE.md` is the short
list of things not to get wrong; this file is the reasoning behind it. API
mechanics — endpoints, payload shapes, the Dutch status strings and the
`observationCode` vocabulary — live in the private `carrier-research/postnl/api/`
and are never copied here.

Three things make this integration unlike the rest of the suite. It is a
**fork** of `arjenbos/ha-postnl` rather than a template bootstrap. It talks to
**three separate APIs behind one bearer token** instead of a single client. And
it ships **MyMail letters with per-letter image entities**, making it the only
carrier in the suite with an `image` platform.

## Project layout

```
custom_components/postnl/
├── __init__.py          setup, runtime_data, first refresh
├── auth.py              PKCE login, token refresh, the auth-error split
├── graphql.py           PostNLGraphql — profile + shipment list
├── jouw_api.py          PostNLJouwAPI — Track & Trace, letters, image bytes
├── login_api.py         PostNLLoginAPI — userinfo
├── letters.py           pure MyMail letter parsing — no I/O, no HA objects
├── parcels.py           pure parcel mapping/normalization — no I/O, no HA objects
├── coordinator.py       poll cycle, caches, dynamic polling, event firing
├── const.py             domain, polling constants, option keys
├── config_flow.py       login, reauth, options
├── sensor.py            summary, per-parcel, letters and diagnostic sensors
├── image.py             PostNLLetterImage — one entity per announced letter
├── button.py            refresh button
├── calendar.py          read-only deliveries calendar
├── device.py            device registry helpers
├── device_trigger.py    six no-code triggers
└── diagnostics.py       redacted diagnostics
```

There is no `api.py` and no `services.py`. The transport is split by API rather
than gathered behind one client, and there are no track/untrack services because
the account feed is the source of parcels.

`parcels.py` and `letters.py` are deliberately free of I/O and HA objects (beyond
the config entry's options) so the mapping logic stays unit-testable without Home
Assistant.

## The three APIs

All three carry the same bearer token minted by `auth.py`.

| Client | Module | Serves |
|---|---|---|
| `PostNLGraphql` | `graphql.py` | `profile()`, `shipments()` — the account's receiver + sender lists |
| `PostNLJouwAPI` | `jouw_api.py` | `track_and_trace(key)`, `letters()`, `image(url)` |
| `PostNLLoginAPI` | `login_api.py` | `userinfo()` |

**`jouw.postnl.nl` is the universal backend — never route to `.be`.** The
GraphQL inbox is account-scoped, not domain-scoped (`.be` returns a
byte-identical list), and MyMail on `.be` returns HTTP 400 because letters are an
NL-only feature. A NL/BE dropdown would therefore be a no-op for parcels and
would break letters — **do not add one.** Belgian accounts are already covered;
the real Belgium gap is bpost.

### Why `requests` and not aiohttp

The API clients use `requests` via executor jobs. This is a deliberate,
documented divergence from the Platinum `async-dependency` / `inject-websession`
rules: converting them would be a large refactor for marginal gain on an
integration that polls a handful of times an hour. Two consequences follow.

**Every `jouw_api` call carries a `(10, 60)` timeout.** `requests` has no
session-level default, so a hanging server would block an executor thread — and
with it the whole refresh — indefinitely.

**API clients are reused across polls**, rebuilt only when the access token
changes (`_api_token`). Each owns a `requests.Session` connection pool that would
otherwise leak on every poll.

`aiohttp.ClientError` is not caught in the coordinator (the coordinator wraps it
automatically); `requests` errors *are* caught, because executor jobs re-raise
them.

### `async_get_jouw_api()` exists because of the image entity

**Anything calling `jouw_api` outside the poll cycle must go through
`coordinator.async_get_jouw_api()`, never read `coordinator.jouw_api`
directly.**

`PostNLLetterImage.async_image()` is the reason. It runs on demand, whenever a
client requests the photo, independently of the poll cycle — so `jouw_api`'s
baked-in bearer token can have expired since the last poll (30 min default
interval; PostNL's own access tokens are not guaranteed to live that long).
Reading the cached client directly produced a 401 on every fetch until the next
poll happened to refresh it. The refresh-then-maybe-rebuild logic used to live
inline in `_async_update_data`; it now lives in `async_get_jouw_api()`, which
`_async_update_data` simply awaits.

`_delivered_history` and `transform_shipment` do read `coordinator.jouw_api`
directly, but only ever run synchronously inside `_async_update_data`'s own call
chain, right after that method's refresh. They are part of the poll cycle, not
an exception to the rule.

## Authentication

**PKCE login with a re-login fallback** (`auth.py`): try a refresh-token
exchange first; on failure re-run the full username/password login; reauth is
the last resort. **The order matters — do not reorder.** This deliberately
avoids HA's `OAuth2Session`, which would re-introduce the browser-extension
onboarding the fork dropped.

`check_and_refresh_token` **preserves the old refresh token** when PostNL's
response omits a new one, and holds an `asyncio.Lock` with a re-check inside, so
two callers never spend the same rotating token twice.

**The auth-error split is load-bearing.** Only a definitive credential rejection
(`PostNLInvalidAuth`) escalates to `ConfigEntryAuthFailed` and reauth. Every
other `PostNLAuthError` — recaptcha, rate-limit, a changed widget, a network
blip — becomes a generic `HomeAssistantError` and so a retryable `UpdateFailed`
or `ConfigEntryNotReady`. This is what stopped the "logged out roughly once a
day" bug; **do not collapse the two paths.**

Reauth guards the account: `reauth_confirm` uses `async_set_unique_id` plus
`_abort_if_unique_id_mismatch`, so a *different* account's credentials abort
rather than silently rebinding.

## Dynamic polling

Rolled out 2026-08-30. `CONF_REFRESH_INTERVAL` accepts the numeric options
`15/30/60/120/240` **plus `"auto"`**. New entries default to `"auto"`
(`DEFAULT_NEW_REFRESH_INTERVAL`); an entry created before the option existed
keeps whatever it already has, numeric or auto (`DEFAULT_REFRESH_INTERVAL` = 30).

For a fixed setting the configured value is the final word. For `"auto"` the
initial interval is merely a starting point — the hot cadence, so the first poll
after setup happens promptly — and `_async_update_data` recomputes it every
refresh via `_next_update_interval`:

- **Quiet window** (`QUIET_WINDOW_START_HOUR` 0 → `QUIET_WINDOW_END_HOUR` 6):
  no polling between those local hours except two daily anchors (00:00 and
  06:00) for overnight and end-of-day catch-up. A candidate time landing inside
  the window is clamped forward to the next anchor — including when *now* is
  already inside it, which is how an anchor poll computes its own follow-up.
- **Hot tier** (`HOT_INTERVAL_MINUTES` 15) when at least one active receiver
  parcel is `out_for_delivery` within `HOT_LOOKAHEAD_HOURS` (1) of its
  `planned_from`, or has no `planned_from` at all.
- **Mid tier** (`MID_INTERVAL_MINUTES` 45) for anything else still in flight.
- **It never fully stops.** This is the account-based model (dynamic-polling.md
  Section 2.2): one account call returns the full receiver, sender *and* letters
  state, so the mid-tier poll is also the only way a new shipment or letter gets
  discovered. Unlike a barcode-based coordinator, `_hottest_tier_minutes` never
  returns `None`.
- **Stagger** (`STAGGER_MINUTES` 7): a deterministic per-install offset — a
  SHA-256 hash of the config entry id, not random — added to every computed
  interval so installs don't all hit an anchor or tier boundary at the same
  second.

## MyMail letters and images

**Letter image URLs require auth**, so `PostNLLetterImage` fetches the bytes
server-side and serves them through HA's authenticated image proxy. **Do not
switch to a redirect scheme.** MyMail also needs app-identification headers that
occasionally need bumping when PostNL ships a new app version.

`postnl_letter_announced` fires per new letter. `_known_letter_ids` mirrors
`_known_state` and is reset only after a *successful* letters fetch.

The first refresh running in `__init__.py` is what guarantees
`coordinator.letters` is populated before `image.py` registers its initial
entities.

## Status mapping

**`map_parcel_status` prefers `observationCode` over the Dutch human string.**
`delivered` short-circuits first; then `derive_observation_status` (in
`parcels.py`) walks the same observation list `build_history` uses — milestone
and meta carry-forward included — and returns the current stage.

That derivation runs on **every** active-path poll, independently of the opt-in
`CONF_INCLUDE_HISTORY`. That option gates only whether the full timeline is
*exposed* on `history`; the underlying observations are always fetched and
always consulted for status.

Only when the derivation returns `None` — no observations, or none recognised —
does it fall back to **ordered substring patterns, more specific first**,
against `statusPhase.message`. The raw string lives on `raw_status`, never
`status`, on either path. Unmapped on both paths gives `ParcelStatus.UNKNOWN`.

This changed after hki-parcels-card discussion #17: PostNL's free text drifts in
wording — five closed "unrecognised status" issues, all the same root cause —
and `observationCode` does not have that failure mode.

Unknown-status warnings fire once per distinct value (parcel status and history
`observationCode` separately), each with an `issues/new` link, tracked in
`_LOGGED_UNKNOWN_STATUSES` / `_LOGGED_UNKNOWN_OBSERVATION_CODES`.

### Milestone vs meta, and carry-forward

Only milestone codes carry a movement status. Meta codes — ETA recalculations,
enrichment events — inherit the previous milestone's stage, so the timeline never
bounces backward on a cosmetic event. The baseline before the first milestone is
`registered`. The one legitimate step back is a real delay or failure. Unmapped
codes stay `null` and do **not** carry forward.

**Do not undo this**, and do not assign a fixed status to ETA codes — that is
wrong by construction.

## Resilience and caching

**One broken parcel no longer fails the refresh.** The active-path Track & Trace
call degrades per parcel: reuse the last good transform from `_parcel_cache`
(pruned each poll), else fall back to GraphQL-only fields. `UpdateFailed` is the
last resort, for when there is nothing to show at all.

**Delivered parcels get history too.** The delivered short-circuit makes the
extra T&T call via `_delivered_history`. It is **non-fatal** — a
`RequestException` yields `None` — and cached per barcode so it happens at most
once per parcel ever. Failures are deliberately **not** cached, so the next poll
retries.

`receiver`, `weight` and `dimensions` come from native grams and millimetres,
converted to canonical kg and cm with the long edge as `length`. Delivered
parcels skip T&T, so all three are `None` for them.

## Events and surfaces

**Incoming events** (`postnl_parcel_registered` / `_status_changed` /
`_delivered` / `_delivery_time_changed`) run over the **full receiver list**,
active plus delivered. A change *to* DELIVERED fires only `_delivered`; an
already-delivered parcel fires nothing; `registered` fires only for
not-yet-delivered new barcodes. `delivery_time_changed` fires only on a non-null
`planned_*` that differs. State lives in `_known_state` /
`_known_delivery_times`.

**Outgoing events** (`postnl_outgoing_parcel_status_changed` /
`_outgoing_parcel_delivered`) run over the **full `data['sender']`** list. Own
shipments *and* returns both land in `senderShipments`, so returns are covered
for free — no `isReturn` filtering as in DHL. `delivered` wins the terminal hop,
and there is **no** outgoing `registered` or `delivery_time_changed`. State lives
in `_known_outgoing_state`.

`device_id` (from `_cached_device_id`) is on every payload.
`device_trigger.py` exposes six no-code triggers: four parcel, plus
`letter_announced` and the outgoing pair.

**Sensor cleanup is sensor-scoped**: filter `domain == "sensor"` before treating
an `{account_id}_*` unique_id as a barcode, else it deletes the refresh button
**and the letter image entities**. `_last_update` and every other non-parcel
`{account_id}_*` sensor must stay in `non_parcel_unique_ids`. Per-parcel sensors
are removed by the summary sensor — the old self-remove raced and left ghosts.

Also shipped: a refresh `button`, a diagnostic `last_update` sensor
(`coordinator.last_success_time`), and a deliveries `calendar` — read-only over
non-delivered receiver parcels, no extra API calls, enabled by default. Letters
are deliberately **not** on the calendar.

**Entities** use `has_entity_name` plus `translation_key` (no `_attr_name`),
icons in `icons.json` and translated units. Device name is `"PostNL (<email>)"`.
`_unrecorded_attributes` keeps parcel and letter lists — and `history` — out of
the recorder.

**Options flow** has no `entry.add_update_listener`; it calls
`async_schedule_reload` on submit. This is the account-based half of the suite's
two options models.

## Fork / upstream relationship

Fork of [`arjenbos/ha-postnl`](https://github.com/arjenbos/ha-postnl),
maintained by [@peternijssen](https://github.com/peternijssen). HACS releases
ship from this fork; fixes that also apply upstream are filed as separate PRs
against `arjenbos/main`. `manifest.json` still lists `@arjenbos` as codeowner.

(The old `CLAUDE.md` pointed at a `CHANGES.md` for cross-repo coordination.
No such file exists in this repo's history — if that coordination log lives
somewhere, record the real location here.)

Branding uses the upstream assets in `home-assistant/brands` — PostNL has a
stable core icon — unlike the other carriers' local `brand/` folders. This is the
one place the suite-wide branding rule does not apply.

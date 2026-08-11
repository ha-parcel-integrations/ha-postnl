# Working in this repository

Home Assistant custom integration for PostNL parcel tracking **plus MyMail
letters and per-letter image entities**. Distributed via HACS; not part of HA
core. **Silver** quality tier, minimum HA `2024.7.0`. A **fork** of
`arjenbos/ha-postnl` (see below). Three APIs behind one bearer token.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, **image entity**, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (the MyMail photos use the [image entity](https://developers.home-assistant.io/docs/core/entity/image) page). Don't rely on memory |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where PostNL deviates) |
| consider "fixing" a lint/pattern the skill flags (poll interval, `requests`/sync, inline client) | *Deliberate skill divergences* — don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `carrier-research/api/postnl/` (private research repo)** — the GraphQL
shipment list, Track & Trace, MyMail (letters + image bytes) and login endpoints,
their payload shapes, the Dutch status strings and the `observationCode`
vocabulary. Do not duplicate them here.

**Suite-wide tripwire, kept inline on purpose:** the first refresh runs in
`__init__.py` *before* `async_forward_entry_setups` — `async_setup_entry` sets
`entry.runtime_data` (the coordinator reads `runtime_data.auth`) then awaits
`coordinator.async_config_entry_first_refresh()`. From a forwarded platform HA
can't catch `ConfigEntryNotReady` and half-sets-up the entry. This also guarantees
`coordinator.letters` is populated before `image.py` registers its initial
entities. Runtime-only; do not move it back into a platform.

## Load-bearing PostNL decisions — do not refactor away

**Auth & token refresh (do not weaken)**
- **PKCE login with re-login fallback** (`auth.py`): try a refresh-token exchange
  first; on failure re-run the full username/password login; reauth is the last
  resort. **Order matters — don't reorder.** Deliberately avoids HA's
  `OAuth2Session` (which would re-introduce the browser-extension onboarding the
  fork dropped).
- `check_and_refresh_token` **preserves the old refresh token** when PostNL's
  response omits a new one, and holds an **`asyncio.Lock`** (with a re-check inside)
  so two callers never spend the same rotating token twice.
- **Auth-error split.** Only a definitive credential rejection (`PostNLInvalidAuth`)
  escalates to `ConfigEntryAuthFailed` / reauth. Any other `PostNLAuthError`
  (recaptcha, rate-limit, changed widget, network blip) → generic
  `HomeAssistantError` → retryable `UpdateFailed` / `ConfigEntryNotReady`. This
  stopped the "logged out ~once a day" bug — do not collapse these.
- **Reauth guards the account**: `reauth_confirm` uses `async_set_unique_id` +
  `_abort_if_unique_id_mismatch` so a *different* account's credentials abort
  instead of rebinding.

**The three APIs (integration behaviour)**
- **Every `jouw_api` call has a `(10, 60)` timeout** — `requests` has no
  session-level default; a hanging server would block an executor thread (and the
  whole refresh) forever.
- **API clients are reused across polls** — rebuilt only when the access token
  changes (`_api_token`); each owns a `requests.Session` connection pool that would
  otherwise leak every poll.
- **Anything that calls `jouw_api` outside `_async_update_data` must go through
  `coordinator.async_get_jouw_api()`, never read `coordinator.jouw_api` directly.**
  That poll method's own token-refresh-then-maybe-rebuild logic now lives in
  `async_get_jouw_api()`; `_async_update_data` just awaits it. `PostNLLetterImage.async_image()`
  is the reason this exists — it runs on demand, whenever a client requests the
  photo, independently of the poll cycle, so `jouw_api`'s baked-in bearer token can
  have expired since the last poll (30 min default interval; PostNL's own access
  tokens are not guaranteed to live that long) — reading the cached client directly
  produced a 401 on every fetch until the next poll happened to refresh it.
- `aiohttp.ClientError` is not caught in the coordinator (wrapped automatically);
  `requests` errors *are* caught (executor jobs re-raise them).
- **`jouw.postnl.nl` is the universal backend — never route to `.be`.** The GraphQL
  inbox is account-scoped, not domain-scoped (`.be` returns a byte-identical list);
  MyMail on `.be` returns HTTP 400 (NL-only feature). A NL/BE dropdown would be a
  no-op for parcels and break letters — **do not add one.** Belgian accounts are
  already covered. The real Belgium gap is **bpost**.

**MyMail letters & images**
- **Letter image URLs require auth** — the `PostNLLetterImage` entity fetches bytes
  server-side and serves them through HA's authenticated image proxy. **Do not
  switch to a redirect scheme.** MyMail also needs app-identification headers that
  occasionally need bumping when PostNL ships a new app version (see `carrier-research/api/postnl/`).
- `postnl_letter_announced` fires per new letter; `_known_letter_ids` mirrors
  `_known_state`, reset only after a successful letters fetch.

**Status mapping & per-parcel resilience**
- **PostNL status is a Dutch human string, not an enum** — `map_parcel_status` uses
  **ordered substring patterns (more specific first)**; the raw string lives on
  `raw_status`, never `status`. Unmapped → `ParcelStatus.UNKNOWN`.
- **`receiver`/`weight`/`dimensions`**: weight/dimensions come from native g+mm
  converted to canonical kg+cm with the long edge as `length`; delivered parcels
  skip T&T → both `None`.
- **One broken parcel no longer fails the refresh.** The active-path T&T call
  degrades per parcel: reuse the last good transform (`_parcel_cache`, pruned each
  poll), else GraphQL-only fields; `UpdateFailed` is the last resort when there's
  nothing to show.
- Unknown-status warnings fire once per distinct value (parcel status +
  history `observationCode`), with an `issues/new` link; one-shot sets
  `_LOGGED_UNKNOWN_STATUSES` / `_LOGGED_UNKNOWN_OBSERVATION_CODES`.

**History (opt-in, default OFF — `CONF_INCLUDE_HISTORY`)**
- **Delivered parcels get history too** — the delivered short-circuit makes the
  extra T&T call via `_delivered_history`. **Non-fatal** (a `RequestException` →
  `None`), cached per barcode (one call per parcel ever); failures are NOT cached
  so the next poll retries.
- **Milestone vs meta + carry-forward (do not undo).** Only milestone codes carry a
  movement status; meta codes (ETA recalcs, enrichment, …) inherit the previous
  milestone's stage so the timeline never bounces backward on a cosmetic event.
  Baseline before the first milestone is `registered`. The one legitimate step-back
  is a real delay/failure. Unmapped codes stay `null` and do NOT carry forward. A
  fixed status for ETA codes is wrong by construction.

**Events, triggers & surfaces**
- Incoming events (`postnl_parcel_registered` / `_status_changed` / `_delivered` /
  `_delivery_time_changed`) run over the **full receiver list** (active +
  delivered): change **to** DELIVERED fires only `_delivered`; already-delivered
  fires nothing; `registered` only for not-yet-delivered new barcodes.
  `delivery_time_changed` only on a non-null `planned_*` that differs. State in
  `_known_state` / `_known_delivery_times`.
- Outgoing (`postnl_outgoing_parcel_status_changed` / `_outgoing_parcel_delivered`)
  run over the **full `data['sender']`** list — own shipments *and* returns both
  land in `senderShipments`, so returns are covered for free. `delivered` wins the
  terminal hop; **no** outgoing `registered` / `delivery_time_changed`. State in
  `_known_outgoing_state`.
- `device_id` on every payload (`_cached_device_id`). `device_trigger.py` exposes
  six no-code triggers (four parcel + `letter_announced` + the outgoing pair).
- **Sensor cleanup is sensor-scoped**: filter `domain == "sensor"` before treating
  an `{account_id}_*` unique_id as a barcode, else it deletes the refresh button
  **and the letter image entities**. `_last_update` (and other non-parcel
  `{account_id}_*` sensors) **must** stay in `non_parcel_unique_ids`.
- **Refresh `button`**, **diagnostic `last_update` sensor**
  (`coordinator.last_success_time`), **deliveries `calendar`** (read-only over
  non-delivered receiver parcels, no extra API calls, enabled by default; letters
  are NOT on it). Per-parcel sensors are removed by the summary sensor (the old
  self-remove raced and left ghosts).
- **Entities**: `has_entity_name` + `translation_key` (no `_attr_name`), icons in
  `icons.json`, translated units; device name `"PostNL (<email>)"`;
  `_unrecorded_attributes` keeps parcel/letter lists (and `history`) out of the
  recorder. **Options flow** has no `entry.add_update_listener` —
  `async_schedule_reload` on submit. `CONF_REFRESH_INTERVAL` = 15/30/60/120/240
  min, default 30.

## Planned / skipped

- **Planned (next major)**: exception translations; per-letter events (e.g.
  `postnl_letter_received`) instead of the watch-the-count workaround.
- **Skipped on purpose**: slimming `extra_state_attributes` (recorder handled);
  `async-dependency` / `inject-websession` (Platinum) — the APIs use `requests`
  via executor jobs, aiohttp would be a big refactor for marginal gain.

## Fork / upstream relationship

Fork of [`arjenbos/ha-postnl`](https://github.com/arjenbos/ha-postnl), maintained
by [@peternijssen](https://github.com/peternijssen). HACS releases ship from this
fork; fixes that apply upstream are filed as separate PRs against `arjenbos/main`.
`manifest.json` still lists `@arjenbos` as codeowner. Cross-repo coordination is in
`CHANGES.md`. Branding uses the upstream assets in `home-assistant/brands` (PostNL
has a stable core icon) — unlike the other carriers' local `brand/`.

## Running tests

```
python -m pytest tests/ --cov=custom_components.postnl
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.

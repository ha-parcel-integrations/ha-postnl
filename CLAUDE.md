# Working in this repository

Home Assistant custom integration for PostNL parcel tracking **plus MyMail
letters and per-letter image entities**. Distributed via HACS; not part of HA
core. **Silver** quality tier, minimum HA `2024.12.0`. A **fork** of
`arjenbos/ha-postnl`. Three APIs behind one bearer token.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before touching `auth.py`, one of the three API clients, the image entity, or the status derivation. It also carries the fork/upstream relationship |
| Endpoint mechanics, payload shapes, status vocabularies | `carrier-research/postnl/api/` (private repo) — the GraphQL shipment list, Track & Trace, MyMail (letters + image bytes) and login endpoints, the Dutch status strings and the `observationCode` vocabulary. **Never** duplicated into this repo |
| Suite-wide conventions | [`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md) |

This file is the short list of things an agent must not get wrong.

## Shared conventions — fetch when relevant

Don't fetch `CONVENTIONS.md` every session — fetch it **before** you act in one
of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, **image entity**, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (the MyMail photos use the [image entity](https://developers.home-assistant.io/docs/core/entity/image) page). Don't rely on memory |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where PostNL deviates) |
| consider "fixing" a lint/pattern the skill flags (`requests`/sync, inline client) | *Deliberate skill divergences* — don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwire, kept inline on purpose:** the first refresh runs in
`__init__.py` *before* `async_forward_entry_setups` — `async_setup_entry` sets
`entry.runtime_data` (the coordinator reads `runtime_data.auth`) then awaits
`coordinator.async_config_entry_first_refresh()`. From a forwarded platform HA
can't catch `ConfigEntryNotReady` and half-sets-up the entry. This also guarantees
`coordinator.letters` is populated before `image.py` registers its initial
entities. Runtime-only; do not move it back into a platform.

## Load-bearing PostNL decisions — do not refactor away

**Auth order is load-bearing** (`auth.py`): refresh-token exchange first, then a
full username/password re-login, then reauth as the last resort. **Don't
reorder.** `check_and_refresh_token` **preserves the old refresh token** when
PostNL omits a new one, and holds an `asyncio.Lock` (re-check inside) so two
callers never spend the same rotating token twice.

**The auth-error split stopped the "logged out once a day" bug — do not collapse
it.** Only `PostNLInvalidAuth` (a definitive credential rejection) escalates to
`ConfigEntryAuthFailed`/reauth. Every other `PostNLAuthError` (recaptcha,
rate-limit, changed widget, network blip) → generic `HomeAssistantError` →
retryable `UpdateFailed`/`ConfigEntryNotReady`. Reauth guards the account with
`async_set_unique_id` + `_abort_if_unique_id_mismatch`.

**Never read `coordinator.jouw_api` directly from outside the poll cycle — use
`coordinator.async_get_jouw_api()`.** The image entity fetches on demand, so the
cached client's baked-in token can be expired; reading it directly produced a 401
on every fetch until the next poll. (`_delivered_history` and
`transform_shipment` read it directly, but run inside `_async_update_data`'s own
chain — part of the poll cycle, not an exception.)

**`jouw.postnl.nl` is the universal backend — never route to `.be`.** The GraphQL
inbox is account-scoped (`.be` returns a byte-identical list); MyMail on `.be`
returns HTTP 400. A NL/BE dropdown is a no-op for parcels and breaks letters —
**do not add one.** The real Belgium gap is bpost.

**Every `jouw_api` call needs its `(10, 60)` timeout** — `requests` has no
session-level default and a hang would block an executor thread, and the whole
refresh, forever. **API clients are reused across polls**, rebuilt only on an
access-token change (`_api_token`); each owns a `requests.Session` pool that
would otherwise leak every poll.

**Letter image URLs require auth** — `PostNLLetterImage` fetches bytes
server-side and serves them via HA's authenticated image proxy. **Do not switch
to a redirect scheme.** MyMail's app-identification headers need bumping when
PostNL ships a new app version.

**`map_parcel_status` prefers `observationCode` over the Dutch free text** —
`delivered` short-circuits, then `derive_observation_status` runs on **every**
active-path poll regardless of `CONF_INCLUDE_HISTORY` (that option gates only
whether `history` is *exposed*). Ordered substring patterns against
`statusPhase.message` are the fallback, only when derivation returns `None`. Raw
string always on `raw_status`, never `status`. PostNL's free text drifts wording
— five closed "unrecognised status" issues, one root cause — so **don't invert
this preference.**

**Milestone vs meta carry-forward — do not undo.** Only milestone codes carry a
movement status; meta codes (ETA recalcs, enrichment) inherit the previous
milestone's stage so the timeline never bounces backward. Baseline before the
first milestone is `registered`. Unmapped codes stay `null` and do NOT carry
forward. **A fixed status for ETA codes is wrong by construction.**

**One broken parcel must not fail the refresh** — the active-path T&T call
degrades per parcel via `_parcel_cache` (pruned each poll), else GraphQL-only
fields; `UpdateFailed` is the last resort. `_delivered_history` is non-fatal and
cached per barcode, but **failures are not cached** so the next poll retries.

**Sensor cleanup is sensor-scoped**: filter `domain == "sensor"` before treating
an `{account_id}_*` unique_id as a barcode, else it deletes the refresh button
**and the letter image entities**. `_last_update` and every other non-parcel
`{account_id}_*` sensor **must** stay in `non_parcel_unique_ids`. Per-parcel
sensors are removed by the summary sensor (self-remove raced and left ghosts).

**Options flow** has no `entry.add_update_listener` — `async_schedule_reload` on
submit. Two sections left, `delivered` and `history`.

**Polling cadence is not configurable — don't add the option back.** The
Section 2.2 account-based algorithm always runs: `_async_update_data` recomputes
`update_interval` at the end of every refresh (quiet window 00:00–06:00 with two
anchors, hot 15 min / mid 45 min, never a full stop because the mid-tier poll is
also how a new shipment or letter is discovered, plus a per-install stagger).
Mail piggybacks on whatever cadence parcels are running at — **no separate
letter cadence.** The `refresh_interval` dropdown (Phase 1, 4.8.0) is gone; a
stale stored value is never read. Full model:
[`ARCHITECTURE.md`](ARCHITECTURE.md).

**Events** — incoming run over the **full receiver list** (active + delivered):
the hop *to* DELIVERED fires only `_delivered`, already-delivered fires nothing,
`registered` only for not-yet-delivered new barcodes. Outgoing run over the
**full `data['sender']`** list — own shipments *and* returns both land in
`senderShipments`, so returns are covered for free; **no** outgoing `registered`
/ `delivery_time_changed`.

## Planned / skipped

- **Planned (next major)**: exception translations; per-letter events (e.g.
  `postnl_letter_received`) instead of the watch-the-count workaround.
- **Skipped on purpose**: slimming `extra_state_attributes` (recorder handled);
  `async-dependency` / `inject-websession` (Platinum) — the APIs use `requests`
  via executor jobs, aiohttp would be a big refactor for marginal gain.

## Running tests

```
python -m pytest tests/ --cov=custom_components.postnl
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README, `ARCHITECTURE.md` and this file in
the same commit; API mechanics go to `carrier-research/postnl/api/`, never here.

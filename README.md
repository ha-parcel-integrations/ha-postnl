# PostNL Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-postnl.svg)](https://github.com/ha-parcel-integrations/ha-postnl/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your PostNL shipments and announced MyMail letters.

Based upon the original work by [@arjenbos](https://github.com/arjenbos/ha-postnl).

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Incoming and outgoing active-parcel count sensors
- Per-parcel sensor per active incoming shipment, with full status details as attributes
- Optional per-parcel status history timeline (opt-in; off by default)
- Configurable delivered-parcels sensor (last N days, or N most recent)
- Next delivery datetime sensor (device class `timestamp`)
- PostNL Punt sensor — parcels destined for a PostNL Point pickup location
- MyMail letters sensor plus a per-letter image entity holding the scanned photo
- Automatic lifecycle management — per-parcel sensors are created and removed as parcels move through delivery
- Re-authentication support — silently refreshes the PostNL token, prompts only when the refresh fails

## Requirements

- Home Assistant 2024.12 or newer
- A [PostNL](https://jouw.postnl.nl) account (the credentials you use on jouw.postnl.nl / the PostNL mobile app)
  - Both **Netherlands and Belgium** PostNL accounts are supported — they share the same backend, so your parcels come through either way. (MyMail letter scanning is a Netherlands-only feature and stays empty for Belgian mail.)

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add this repository URL and select category **Integration**
3. Search for **PostNL** and install it
4. Restart Home Assistant

### Manual

1. Copy the `postnl` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **PostNL**
3. Enter your PostNL **email address** and **password**
4. Choose how you want the **delivered parcels** sensor to filter (last N days, or N most recent)
5. Click **Submit**

### Setup parameters

| Field | Description |
|---|---|
| Email | The email address of your PostNL account. |
| Password | The password for that account. Stored in the HA config entry and refreshed automatically when the integration triggers a re-authentication. |

## Options

Click **Configure** on the integration card. The form is split into three
sections:

### Delivered parcels

| Option | Description |
|---|---|
| Filter by | `Days` keeps delivered parcels visible for the last N days. `Number of parcels` keeps only the N most recent regardless of age. |
| Amount | The N used by the filter above. |

### Parcel history

| Option | Description |
|---|---|
| Include status history | Adds a `history` attribute to each parcel — the ordered list of status updates (timestamp, canonical status, original PostNL text), capped to the most recent 20. **Off by default.** The attribute is kept out of the recorder database. |

### Polling

| Option | Description |
|---|---|
| Refresh every | How often the integration checks PostNL. Choices: **15 / 30 / 60 / 120 / 240 minutes** — default 30. A slower interval is gentler on PostNL's API. Changes take effect immediately, no HA restart needed. |

## Removal

Standard HA removal applies: **Settings → Devices & Services →
PostNL → ⋮ → Delete**. No PostNL-side cleanup is needed; deleting the
config entry stops the polling. To revoke API access entirely, change
your PostNL account password — the integration will trigger a re-auth
notification, which you can then ignore.

## Sensors

The integration creates one device per PostNL account, named
**`PostNL (<your-email>)`**. With multiple accounts each gets its own
device named after its email. The entities below show the
friendly-name pattern; their entity_ids carry the same account suffix:

| Friendly name pattern | Description |
|---|---|
| `PostNL (account) Incoming parcels` | Number of active incoming parcels |
| `PostNL (account) Parcel <barcode>` | Canonical status of a single incoming shipment |
| `PostNL (account) Next delivery` | Earliest expected delivery datetime |
| `PostNL (account) En route to PostNL Point` | Active incoming parcels destined for a PostNL Point pickup location |
| `PostNL (account) Delivered parcels` | Recently delivered incoming parcels (configurable window) |
| `PostNL (account) Outgoing parcels` | Number of active outgoing parcels |
| `PostNL (account) Outgoing delivered parcels` | Recently delivered outgoing parcels (same configurable window) |
| `PostNL (account) Letters` | Letters announced by PostNL's MyMail service over the last ~2 weeks; `unread` count and `letters` list on attributes |
| `PostNL (account) Letter <title>` (image entity) | Scanned photo of a single announced letter, fetched with your token and served through Home Assistant. Attributes mirror the sensor's letter dict: `id`, `title`, `date`, `unread` |

A **`PostNL (account) Deliveries`** calendar entity is also created, showing
expected delivery dates for active incoming parcels — read-only, no extra
API calls, letters are not included.

A **`PostNL (account) Refresh`** button entity forces an immediate poll,
without waiting for the next scheduled interval.

Every parcel exposed on a sensor attribute uses a carrier-agnostic shape:

| Key | Type | Meaning |
|---|---|---|
| `carrier` | string | `"PostNL"` |
| `barcode` | string | Parcel tracking number |
| `sender` | string \| null | Sender name (e.g. webshop) |
| `receiver` | string \| null | Recipient name |
| `status` | `ParcelStatus` | Canonical status — see the [status reference](#parcel-status-reference) |
| `raw_status` | string \| null | Original PostNL status description (a Dutch human-readable string) |
| `delivered` | bool | Whether the parcel has been delivered |
| `delivered_at` | ISO 8601 \| null | Delivery moment, if known |
| `planned_from` | ISO 8601 \| null | Expected delivery window start |
| `planned_to` | ISO 8601 \| null | Expected delivery window end |
| `pickup` | bool | Destined for a PostNL Point rather than a home address |
| `pickup_point` | string \| null | PostNL Point name when `pickup` is true (always `null` for now — PostNL does not expose the field) |
| `url` | string \| null | Deep link to the parcel's tracking page on jouw.postnl.nl |
| `weight` | float \| null | Parcel weight in kilograms. May be `null` for delivered parcels or when PostNL has not yet attached the data. |
| `dimensions` | dict \| null | Parcel dimensions in centimeters: `{length, width, height, text}` where `text` is a pre-formatted `"L x W x H cm"` string. Same coverage as `weight`. |
| `history` | list \| null | Ordered status timeline (oldest → newest), each entry `{timestamp, status, raw_status}`, capped to the most recent 20. `null` unless the **Parcel history** option is enabled — see [Options](#options). |
| `raw` | dict | The original PostNL payload |

Each announced letter is exposed as an `image` entity so the scan
loads directly in dashboards and notifications. See
[`examples/automations/notify_when_letter_arrives.yaml`](examples/automations/notify_when_letter_arrives.yaml)
for a ready-to-paste notification.

## Parcel status reference

`status` on every parcel is one of the canonical `ParcelStatus` values
below. Use these in your automations rather than PostNL's raw Dutch
description — the raw value stays available on `raw_status` for power
users.

| `status` | Meaning | PostNL signal that maps here |
|---|---|---|
| `registered` | PostNL knows about the label but the parcel is not yet in transit | `statusPhase.message` containing "aangemeld" or "verwacht" |
| `in_transit` | Picked up; somewhere in PostNL's network | `statusPhase.message` containing "onderweg", "ontvangen" or "gesorteerd" |
| `out_for_delivery` | On the delivery vehicle today | `statusPhase.message` containing "wordt vandaag bezorgd", "onderweg naar het bezorgadres" or "onderweg naar de bezorger" |
| `at_pickup_point` | Arrived at the chosen PostNL Point, ready to be collected | `statusPhase.message` containing "ligt klaar bij postnl punt" or similar |
| `delivered` | Handed over (mailbox, recipient, neighbour, picked up) | `shipment.delivered == true` (authoritative); fallback `statusPhase.message` containing "bezorgd" |
| `returning` | Failed delivery, on the way back to the sender | `statusPhase.message` containing "retour" or "teruggestuurd" |
| `unknown` | Raw description we have not mapped yet | anything else — logged once at warning level with a ready-to-paste issue link so it can be added to the map |

Because PostNL's `statusPhase.message` is a human-readable Dutch string
(not a stable API enum), the mapping uses ordered substring matching
— so minor wording variants still resolve correctly. If you see an
`unknown` for a status the integration ought to recognise, open an
issue with the raw value (visible in the integration debug logs and on
the parcel sensor under `raw_status`).

## Events

The coordinator fires events on the HA event bus when something
interesting happens to a parcel, so automations can react without
polling per-parcel sensors.

| Event | When | Payload |
|---|---|---|
| `postnl_parcel_registered` | A new barcode appears in the active list | The full parcel dict (see the table above) |
| `postnl_parcel_status_changed` | A known barcode's `status` value changes, except the final hop to delivered | Same payload plus `old_status` and `new_status` |
| `postnl_parcel_delivered` | An incoming parcel is delivered | The full parcel dict |
| `postnl_parcel_delivery_time_changed` | A known barcode's expected delivery time changes to a new value | Same payload plus `old_planned_from`, `new_planned_from`, `old_planned_to`, `new_planned_to` |
| `postnl_outgoing_parcel_status_changed` | A known **outgoing** parcel (something you sent, or a return) changes status, except the final hop to delivered | Same payload plus `old_status` and `new_status` |
| `postnl_outgoing_parcel_delivered` | An outgoing parcel reaches the recipient | The full parcel dict |
| `postnl_letter_announced` | A new letter appears in the MyMail feed | The letter dict (`id`, `title`, `date`, `unread`, `image_url`) plus `carrier: "PostNL"` |

Every payload also carries a `device_id` identifying the PostNL account
the parcel or letter belongs to, so automations can tell two accounts apart.

Events do not fire for parcels or letters that were already in your account when HA first started.

If you build automations in the UI, these same events are also available
as no-code **device triggers** (**Settings → Automations → Create → Add
trigger → Device**), scoped to the selected account's device. The raw
events above are there for templates and YAML automations.

See [`examples/automations/`](examples/automations/) for ready-to-paste
event-driven automations, or the
[parcel aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator)
for a carrier-agnostic re-emit layer that fires
`parcel_aggregator_parcel_*` events covering every installed carrier
in one go.

## Examples

Ready-to-paste automations and dashboard cards live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

To capture verbose information about the PostNL API responses (useful
when reporting a bug or helping map a new status value), enable debug
logging for the integration:

1. Add this to your `configuration.yaml`:
   ```yaml
   logger:
     default: warning
     logs:
       custom_components.postnl: debug
   ```
2. Restart Home Assistant.
3. Wait for the next poll cycle (or reload the integration from **Settings → Devices & Services → PostNL → ⋮ → Reload**).
4. Open **Settings → System → Logs**, filter for `postnl`, and copy the relevant log lines (including the `Shipments fetched: ...` summary and any `Track and Trace response: ...` payload) into your bug report or message to the maintainer.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `invalid_auth` error during setup | Wrong email or password |
| `cannot_connect` error during setup | PostNL API is unreachable; check your network |
| Re-authentication prompt appears | PostNL session expired and could not be refreshed silently; log in again |
| Sensors disappear after delivery | Expected — delivered parcels move to the delivered sensor (visible window controlled by the options filter) |
| Sensors not updating | Check **Settings → System → Logs** for `postnl` entries |

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project with no affiliation,
endorsement, or connection to PostNL or any of its subsidiaries. The
PostNL API used here is undocumented (reverse-engineered from the
mobile app and jouw.postnl.nl) and may change without notice. The
maintainers have not asked PostNL for permission to use this API;
installing this integration may breach PostNL's Terms of Service.
You take any risk that follows — account suspension, service
disruption, etc. No warranty (see [LICENSE](LICENSE.md)).

## Contributing

This fork is maintained by [@peternijssen](https://github.com/peternijssen).
The original integration is by
[@arjenbos](https://github.com/arjenbos) — fixes that apply to both
forks are filed as PRs against the upstream
[`arjenbos/ha-postnl`](https://github.com/arjenbos/ha-postnl).

Pull requests and issues are welcome. Please open an issue before submitting a large change.

## License

MIT

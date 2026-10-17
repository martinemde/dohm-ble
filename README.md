# Marpac Dohm — Home Assistant integration

Control a Bluetooth **Marpac Dohm** sound machine from Home Assistant as a
**media player** (on/off + volume) or a **10-step fan**. Choose one at setup. The BLE protocol was reverse-engineered
from the official app; see [`docs/protocol.md`](docs/protocol.md), the
[`docs/reverse-engineering.md`](docs/reverse-engineering.md) runbook, and the
[Android APK findings](docs/android-app.md).

> **0.4.4:** sound volume now rounds to the nearest hardware level, with
> halfway values rounding up: 4% → off, 5% → 10%, 44% → 40%, 45% → 50%.
> Rounding to off preserves the previous speed. Fan percentage rounding is
> unchanged. Update through HACS and restart Home Assistant.
>
> **0.4.3:** fixes power/volume state snapping back after a command. Controls
> update on acknowledgment; readback is debounced until one second after the
> last command completes, so rapid taps do not query unsettled state. Update
> through HACS and restart Home Assistant. Keep your configured entries and bonds.
>
> **0.4.2:** restores 0.3.0's normal BLE connection behavior after 0.4.0/0.4.1
> added a required pairing step that can block existing devices. Update through
> HACS and restart Home Assistant. Keep your configured entries; try reconnecting
> without pressing the top button or clearing bonds first. This patch does not
> repair keys already replaced or cleared. The other 0.4 features are retained.
>
> **Existing installs:** entries without an entity-type choice keep their
> current media-player entity type. To choose a fan, delete and re-add the
> integration as described below. Upgrading from the old 0.1.x fan releases can
> leave an unavailable `fan.*` entry; remove it from **Settings → Devices &
> Services → Entities** if you no longer use it. Update automations and cards
> whenever you change between `media_player.*` and `fan.*`.

## Install (HACS)

Download and install through HACS (Home Assistant Community Store):

[![Open Marpac Dohm in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=martinemde&repository=dohm-ble&category=integration)

1. In HACS, add this repository as a **custom repository** (category:
   *Integration*).
2. Install this repository and restart Home Assistant.
3. For initial pairing, close the phone app and **hold the Dohm's top button
   for about five seconds**. This can replace an existing controller's bond.
4. Select the discovered **Yogasleep Dohm Connect**. If it is not discovered,
   use **Settings → Devices & Services → Add Integration → Yogasleep Dohm
   Connect** while the device is in pairing mode.
5. Choose **Media player (volume)** or **Fan (10 speed steps)**. Give it a
   name and, optionally, an area. The name becomes the device/entity name used
   by voice assistants. HA reuses a working bond for normal reconnection.

> **Protect a working pairing.** On the tested device, only one controller
> connection/bond was usable at a time. A new pairing or a top-button hold can
> replace the old bond. These are hardware observations, not guarantees for
> every firmware. Normal reconnection should not require the button. Stop the
> phone app before using HA; press the button only for deliberate setup/recovery.

## Choose the entity type at setup

Fan controls naturally fit the ten speed steps, but HA groups fans with climate
controls. A `media_player` fits sound and volume better, though its 0–100%
volume control must snap to the machine's ten hardware levels.

- **Media player (default):** standard volume controls and volume voice intents.
  Volume below 5% turns the machine off without changing its hardware level. Turn on
  resumes that level: 30% → 0 → turn on returns to 30%. With no known previous
  level, turn on starts at 10%. Volume rounds to the nearest hardware level,
  with halfway values rounding up: 4% → off, 5% → 10%, 44% → 40%, 45% → 50%.
  A setting that rounds above zero powers the machine on.
- **Fan:** ten speed levels, shown as 10%, 20%, …, 100%. Fan percentage 0 turns
  the machine off. A positive setting powers it on and snaps up to a real level.

Only one entity is created. Both types use the same BLE client and pairing
recovery. This choice is **not available in Configure/options**.

To change type, delete the integration entry, add it again, and select the new
type. This changes the entity domain/ID (`media_player.*` ↔ `fan.*`), so update
related automations, scripts, dashboards, and voice-assistant settings.
Deleting an HA entry does not itself clear a controller's bond. If that bond
still works, do not press the pairing button merely to change the entity type.

## Troubleshooting

- **Connected, but no replies.** Allow a few minutes for the first exchange.
  Then check reachability, competing clients, and authentication/notification
  logs. Silence alone does not prove a stale key. If deliberate recovery is
  needed, hold the top button for about five seconds and use **Configure →
  Clear the pairing and pair again**.
- **Moved to a different proxy.** HA can select another controller. Repair a
  stale key with the same deliberate recovery action. If HA switches
  controllers during recovery, it reports partial completion; retry on the
  newly selected controller.
- **Already configured.** Show disabled entries under **Settings → Devices &
  Services**, then re-enable the intended entry. Delete it only if you really
  want a fresh configuration; deleting an entry is not bond recovery.
- **Still unavailable.** Start with [current troubleshooting](docs/debugging.md)
  and the [pairing/recovery guide](docs/pairing.md).

The integration never clears a bond automatically after failed polls.

## What you get

- On/off plus the machine's **10 hardware levels**. Media-player volume rounds
  to the nearest level (halfway rounds up); fan percentages round up to the
  next level.
- **Media player:** device class *speaker*, volume up/down by one hardware
  level, and standard volume intents such as *"set the white noise to 40
  percent"*. Setting volume to 5% or above while off turns the machine on.
  Setting volume below 5% turns it off and preserves its last hardware level.
  It plays no media and offers no transport controls or playback target.
- **Fan:** standard fan on/off and speed controls with ten discrete steps.
  Turn on without a percentage resumes the current hardware speed.
- A **device** entry with manufacturer Yogasleep (formerly Marpac) and model
  Dohm Connect. Firmware/serial reads and a battery sensor are not implemented.

Scheduling is intentionally left to Home Assistant automations even though the
device has schedule slots. The device's onboard clock drifts over time and loses
configuration during power outages. Home assistant is a much more reliable
scheduler.

The standalone client can read and deliberately write onboard schedules; HA
does not sync them or the device clock.

## Requirements

- A Bluetooth adapter on the Home Assistant host **or** an ESPHome Bluetooth
  Proxy within range of the Dohm. Only deliberate proxy bond recovery needs
  the `PAIRING` feature (ESPHome 2024.3.0 or newer); ordinary connections do not
  explicitly request pairing. No external Python packages — the integration is
  self-contained and uses Home Assistant's bundled Bluetooth stack.

The [pairing audit](docs/pairing.md) explains the connection sequence, backend
limits, and a read-only check for a bond that survives reconnecting.

## Standalone client

The full BLE engine is also usable without Home Assistant from this checkout:

```sh
uv sync
uv run python your_script.py
```

Import `DohmClient` from `custom_components.dohm.client`. Omit `device_id` to
query the real ID on connect. The client includes power/speed, device names,
clock writes, schedule reads, and serialized schedule writes with readback.
Experimental clock queries and ID writes use inferred syntax, not proven
firmware support. No separate package or publishing step is needed
(`pyproject.toml` sets `package = false`).

See the [standalone API guide](docs/library.md) for a read-only Bleak scanner
example, deliberate write examples, errors, and firmware/reply limits. This
does not add HA features or automatic name, clock, schedule, or ID changes.
Standard GATT firmware, serial, and battery reads remain unimplemented.

## Developing

The reverse-engineering engine (`protocol.py`, `client.py`) stays vendored inside
`custom_components/dohm/` and is unit-tested without hardware:

```sh
uv run pytest
```

See [contributor instructions](CLAUDE.md) for validation and releases.
`extract_writes.py` decodes captures. `probe.py` and `explore.py` are historical
experiments, not supported pairing diagnostics; `explore.py` changes device
settings. Read the [tool limitations](docs/reverse-engineering.md#tool-limitations)
before using them.

## License

[MIT](LICENSE).

# Marpac Dohm — Home Assistant integration

Control a Bluetooth **Marpac Dohm** sound machine from Home Assistant as a
**media player** (on/off + volume). The BLE protocol was reverse-engineered
from the official app; see [`docs/protocol.md`](docs/protocol.md), the
[`docs/reverse-engineering.md`](docs/reverse-engineering.md) runbook, and the
[Android APK findings](docs/android-app.md).

> **Upgrading from 0.1.x?** The Dohm used to be a `fan` entity. It is now a
> `media_player`, because what you adjust on a sound machine is loudness, not
> airflow — and volume is something the rest of Home Assistant already
> understands. **This changes the entity ID**, so any automation, script, or
> dashboard card referring to `fan.<your dohm>` needs to point at
> `media_player.<your dohm>` instead. The old `fan.*` entity is left behind in
> the registry as unavailable — delete it yourself from **Settings → Devices &
> Services → Entities**.

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
5. Give it a name and, optionally, an area. The name becomes both the device and
   the entity name, so it is what voice assistants listen for — worth naming
   after the room if you have more than one. After this one-time pairing, Home
   Assistant reconnects on its own.

> **Protect a working pairing.** On the tested device, only one controller
> connection/bond was usable at a time. A new pairing or a top-button hold can
> replace the old bond. These are hardware observations, not guarantees for
> every firmware. Normal reconnection should not require the button. Stop the
> phone app before using HA; press the button only for deliberate setup/recovery.

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

- A **media player** entity (device class *speaker*): turn on/off and set volume
  across the device's 10 levels. Volume up/down changes exactly one hardware
  level (10 percentage points), so every press is a real device setting, and
  the control snaps immediately to the level the Dohm took — drop the slider at
  43% and it moves to 50% right away rather than after a round-trip.
- Voice control via the standard volume intents — *"set the white noise to 40
  percent"*, *"turn the white noise down"* — which a fan entity can't answer.
- A **device** entry with manufacturer Marpac and model Dohm. Firmware/serial
  reads and a battery sensor are not implemented.

The Dohm has no silent setting, so volume 0 is the quietest level rather than
off; use turn off for silence. It plays no media, so no transport controls,
sources, or metadata are exposed and it is never offered as a playback target.

Scheduling is intentionally left to Home Assistant automations rather than the
device's onboard timer — far more flexible, and it sidesteps the device's
drifting onboard clock.

## Requirements

- A Bluetooth adapter on the Home Assistant host **or** an ESPHome Bluetooth
  Proxy within range of the Dohm. Explicit proxy pairing needs the `PAIRING`
  feature (ESPHome 2024.3.0 or newer) and a current Home Assistant Bluetooth
  stack. No external Python packages — the integration is self-contained and
  uses Home Assistant's bundled Bluetooth stack.

The [pairing audit](docs/pairing.md) explains the connection sequence, backend
limits, and a read-only check for a bond that survives reconnecting.

## Developing

The reverse-engineering engine (`protocol.py`, `client.py`) is vendored inside
`custom_components/dohm/` and unit-tested without hardware:

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

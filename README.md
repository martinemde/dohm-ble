# Marpac Dohm — Home Assistant integration

Control a Bluetooth **Marpac Dohm** sound machine from Home Assistant as a
**fan** entity (on/off + speed 1–10). The BLE protocol was reverse-engineered
from the official app; see [`docs/protocol.md`](docs/protocol.md) and the
[`docs/reverse-engineering.md`](docs/reverse-engineering.md) runbook.

## Install (HACS)

1. In HACS, add this repository as a **custom repository** (category:
   *Integration*).
2. Install **Marpac Dohm** and restart Home Assistant.
3. The Dohm should be auto-discovered. If not, go to **Settings → Devices &
   Services → Add Integration → Marpac Dohm**.
4. **Hold the top button on the Dohm for ~5 seconds** to make it discoverable,
   then select it. After this one-time pairing, Home Assistant reconnects on its
   own.

> **One controller at a time.** The Dohm stores exactly one pairing, and the top
> button *grants a new one*, replacing whatever was there. That button is only
> for pairing — it plays no part in normal operation, so pressing it while
> troubleshooting will break a working setup. If the phone app has the pairing,
> forget the Dohm there before adding it here.

## Troubleshooting

- **It connects but never responds.** The Dohm only replies over a paired
  (encrypted) link, so this means the stored pairing no longer matches. Hold the
  top button ~5 seconds; the integration clears the stale pairing and re-pairs on
  its own within about a minute, and raises a repair notification if it can't.
- **Adding it does nothing / says it's already configured.** An existing config
  entry is blocking the add, and disabled entries are hidden by default. Turn on
  *show disabled entries* under **Settings → Devices & Services**, then delete
  the old one before re-adding.
- **It went unavailable and stayed there.** Check the logs for
  `custom_components.dohm`; the version history and diagnostic recipes are in
  [`docs/debugging.md`](docs/debugging.md).

## What you get

- A **fan** entity: turn on/off and set speed across the device's 10 levels
  (mapped to 0–100%).
- A proper **device** entry (manufacturer Marpac, model Dohm), with firmware/
  serial pulled from the device when available.

Scheduling is intentionally left to Home Assistant automations rather than the
device's onboard timer — far more flexible, and it sidesteps the device's
drifting onboard clock.

## Requirements

- A Bluetooth adapter on the Home Assistant host **or** an ESPHome Bluetooth
  Proxy within range of the Dohm. No external Python packages — the integration
  is self-contained and uses Home Assistant's bundled Bluetooth stack.

## Developing

The reverse-engineering engine (`protocol.py`, `client.py`) is vendored inside
`custom_components/dohm/` and unit-tested without hardware:

```sh
uv run pytest
```

`tools/` holds the BLE exploration helpers used to map the protocol
(`probe.py`, `explore.py`, `extract_writes.py`).

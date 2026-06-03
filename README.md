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

# Design: `dohm-ble` + Home Assistant integration

**Date:** 2026-06-01
**Status:** Approved (brainstorming complete; ready for implementation plan)

## Goal

Reverse-engineer the BLE protocol of a **Marpac Dohm** sound machine and expose
**power on/off** and **speed** control to **Home Assistant** as a native
`fan` entity.

## Background / what we already know

- The device is built on a **TI BLE module** (the characteristic UUID is a
  TI SensorTag-family UUID), which means commands ride a simple
  serial-over-GATT bridge.
- Communication is **plaintext UTF-8**, grammar `COMMAND,VALUE$` with `$` as
  the terminator.
- Confirmed exchanges, observed manually via LightBlue:
  - `S,03$` reports/sets speed 3
  - the device replies `OK$` after accepting a command
- **Service/characteristic:** a single characteristic that supports both
  **notify** and **write**:
  `00005600-D102-11E1-9B23-00025B005AA5`
- On macOS, the peripheral is addressed by the CoreBluetooth UUID
  `AA114B7E-92CF-F378-B56D-5D6D1654404B`. **This is a per-Mac identifier, not a
  real BLE MAC**, and will NOT carry over to the Home Assistant host — the HA
  side must rediscover the device by its BLE local name.
- The device has an **onboard real-time clock** and a **3-slot schedule**
  (each slot has a start and stop time), persisted on the device. The clock
  **drifts** (including across DST), so the official app performs a periodic
  time-sync.

## Scope

### In scope (v1)
- Power on/off
- Speed selection (the `S,0N$` command family)
- Home Assistant `fan` entity: on/off + percentage-mapped speed

### Out of scope (v1) — deliberate YAGNI
- The device's **onboard schedule** (3 slots) and **clock time-sync**.
  Rationale: in Home Assistant, scheduling belongs to HA automations
  (sun/presence/conditional), which are strictly more capable than 3 fixed
  on-device slots, and bypassing the onboard clock sidesteps the drift problem
  entirely. The schedule + time commands are also the only commands that are
  not brute-forceable from the Mac (they encode times/slots), so excluding them
  removes the need for iOS BLE sniffing in v1.
- If onboard-schedule read/write is ever wanted, it is a separate,
  sniff-required mini-project layered on the same library.

## Discovery strategy

**Probe-first from the Mac; the iPhone app is a reference, not a sniffing
target.** The user can already drive the device by hand in LightBlue and observe
`OK$`/`S,0N$` responses; `bleak` on the Mac automates exactly that loop. iOS HCI
sniffing (Apple Bluetooth logging profile + `sysdiagnose` -> PacketLogger ->
Wireshark) is possible but high-friction and unnecessary for the in-scope
commands, all of which are simple enough to confirm by probing.

## Architecture

Layered so the protocol logic is pure and fully testable without hardware.

### Component 1 — `dohm-ble` standalone Python library (this repo)

```
dohm/
  protocol.py   # pure encode/decode functions; NO bluetooth. The TDD core.
  client.py     # async bleak wrapper: connect, write, await notification
  const.py      # UUIDs and command-grammar constants
tools/
  probe.py      # interactive REPL: send arbitrary strings, log responses
tests/
  test_protocol.py  # pure unit tests, no hardware required
```

- **`protocol.py`** — pure functions only. Examples:
  - `speed(3) -> b"S,03$"`
  - `power(True) -> b"..."` (exact form confirmed in Phase 1)
  - `parse(b"OK$") -> Ack`
  - `parse(b"S,03$") -> SpeedReport(3)`
  Frozen only after Phase 1 probing confirms the real commands. We do not ship
  guesses.
- **`client.py`** — thin async `bleak` wrapper. Connects by CoreBluetooth UUID
  on macOS (dev) and by BLE local name on the HA host (prod). Writes to the
  characteristic and awaits the corresponding notification.
- **`const.py`** — service/characteristic UUIDs, terminator, command prefixes.
- **`tools/probe.py`** — interactive exploration tool used to map the
  vocabulary before the protocol module is frozen.

### Component 2 — Home Assistant custom component (same repo, HACS-installable)

```
custom_components/dohm/
  manifest.json   # bluetooth matcher; deps: dohm-ble, bleak-retry-connector
  config_flow.py  # auto-discovery by BLE local name
  fan.py          # FanEntity: on/off + percentage speed
  __init__.py
  const.py
```

## Data flow

HA `bluetooth` integration discovers the device by local name -> config flow
creates the entry -> a coordinator opens a `bleak` connection via
`bleak-retry-connector` (transparently using any ESPHome Bluetooth Proxy) ->
the `fan` entity maps on/off -> power command and percentage -> speed N ->
writes UTF-8 to characteristic `00005600-...5AA5` -> reads the `OK$` / `S,0N$`
notification to confirm and to track state.

## Unknowns to resolve in Phase 1 (via the probe tool)

1. **Power command** — hypothesis `P,1$` / `P,0$`; confirm actual form.
2. **Speed range + padding** — `03` implies 2-digit zero-padded; confirm the
   max speed (`speed_count` for HA).
3. **Status query** — does `?$` / `S?$` (or similar) dump current state? Useful
   for initial entity state on (re)connect.
4. **Unsolicited notifications** — does the device push state changes on its
   own (e.g. when toggled physically)?

Output of Phase 1: a documented command table committed to the repo.

## Error handling

- `bleak-retry-connector` handles flaky connects and BT-proxy hops.
- Every command awaits its `OK$`/state echo with a timeout; missing echo is
  raised/logged rather than silently assumed successful.
- The HA entity reports `unavailable` when the device is out of range; HA
  recovers automatically on rediscovery.

## Testing

- **`protocol.py`** — comprehensive TDD: positive (`speed(3) == b"S,03$"`) and
  negative (out-of-range speed, malformed parse input) cases. No hardware.
- **`client.py`** — mocked-`bleak` unit tests plus a manual integration check
  via the probe tool against the real device.
- **HA component** — HA's pytest harness with the library mocked.

## Deployment (Home Assistant)

Native HA `bluetooth` integration path. Requires a BLE radio in range of the
device:

- If the HA host's adapter is in range, it works directly.
- Otherwise add a cheap **ESP32 ESPHome Bluetooth Proxy** near the machine;
  `bleak-retry-connector` uses it transparently.

The exact HA host BT/proxy situation is confirmed in Phase 3.

## Phased delivery

1. **Probe & map** — build `tools/probe.py`, explore, produce the documented
   command table.
2. **Library** — TDD `protocol.py`, then `client.py`; verify against the
   device.
3. **HA component** — `fan` entity + config flow.
4. **Deploy** — install in HA; add an ESP32 BT proxy if range demands it.

## Tooling

- Python toolchain via **mise**.
- Dependencies via **uv**.
- Tests via **pytest**.
- Version control via **jj** (colocated git).
- Key libraries: `bleak`, `bleak-retry-connector`; HA's `homeassistant` test
  harness for the component.

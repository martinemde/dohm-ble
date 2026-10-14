# Standalone client library

The BLE engine stays in `custom_components/dohm/client.py` and
`custom_components/dohm/protocol.py`. It imports without Home Assistant.
There is no separate package or publishing step: `pyproject.toml` sets
`package = false`. Use this checkout with Python 3.13 or newer:

```sh
uv sync
uv run python read_dohm.py YOUR_DISCOVERED_ADDRESS
```

Run from the repository root so the vendored modules are on the import path.
Standalone Bleak uses the host's Bluetooth backend, not HA's ESPHome proxies.
See [pairing](pairing.md) for backend limits. Close the phone app and avoid a
second controller connection. A deliberate pairing or top-button hold can
replace a working bond. A successful connection is not proof of a durable bond.

## Read-only example

Save this as `read_dohm.py` at the repository root. With no address argument it
only lists nearby devices. Copy the intended device's address into a second
run; on macOS this is a host UUID, not a MAC address. The connected path reads
state only, but `connect()` can request BLE pairing. Do not use it to replace
another controller's bond without permission.

```python
import asyncio
import sys

from bleak import BleakScanner
from custom_components.dohm.client import DohmClient


async def main():
    devices = await BleakScanner.discover(timeout=10.0)
    for device in devices:
        print(device.address, device.name)
    if len(sys.argv) < 2:
        return
    device = next((d for d in devices if d.address == sys.argv[1]), None)
    if device is None:
        raise SystemExit("Device not found; scan again within range")
    client = DohmClient(device)  # No guessed ID; connect queries i$.
    try:
        await client.connect()
        print("ID:", client.device_id)
        print("Name:", await client.get_name())
        print("Power:", await client.get_power())
        print("Speed:", await client.get_speed())
        for schedule in await client.get_schedules():
            print(schedule)
    finally:
        await client.disconnect()


asyncio.run(main())
```

## Client API

Import `DohmClient` from `custom_components.dohm.client`. All methods below are
async; properties are not. Call `connect()` before state operations.

`DohmClient(ble_device, *, device_id=None, connector=None,
ble_device_callback=None)` takes a discovered Bleak device. The optional
connector is an async callable taking that device and returning a connected
Bleak-compatible client. The optional synchronous device callback is forwarded
to the default retry connector; it is not a rediscovery guarantee.

- `connect(ble_device=None) -> None`: connect and enable notifications. If the
  cached ID is `None`, send `i$` and retain its reply. A supplied ID avoids this
  opening query. HA supplies its hardware-observed MAC-derived ID; standalone
  users should normally omit it. Reconnect retains an already known ID.
- `disconnect() -> None`: release notifications and the connection. It does not
  clear a bond.
- `device_id: str | None` and `is_connected: bool`: cached ID and link state.
- `get_device_id() -> str`: always query `i$` and retain the actual reported ID.
  This reads the ID; it does not change it.
- `rebond(ble_device=None) -> str`: deliberate bond recovery, not a diagnostic
  read. It can clear a working bond and reports partial recovery as text.
  Backend limits and controller guards are in [pairing](pairing.md).

### Captured or app-derived operations

These commands have capture or used Android builder evidence. That is not a
promise that every firmware accepts them. See [protocol](protocol.md) and
[APK provenance](android-app.md) for the evidence and remaining hardware checks.

| Method | Result / parameters |
|---|---|
| `get_power()` | `bool` |
| `set_power(on)` | `None`; `on` must be `bool` |
| `get_speed()` | `int`, 1–10 |
| `set_speed(speed)` | `None`; integer 1–10, not a percentage; does not itself send a power command |
| `get_name()` | `str`, the device's own name |
| `set_name(name)` | `None`; changes the device name, not an HA entity name |
| `set_time(value, weekday)` | `None`; `datetime.time` and ISO weekday, Monday=1 … Sunday=7 |
| `get_schedule(slot)` | `protocol.ScheduleReport`; slot 1–3 |
| `get_schedules()` | Tuple of reports for slots 1–3, read under one operation lock |
| `set_schedule(slot, start, stop, days, speed)` | Verified `protocol.ScheduleReport`; details below |

Names and IDs must be nonempty strings without `,`, `$`, or control characters.
The Android UI allows ten alphanumeric name characters; the encoder does not
impose that unproven firmware limit. Library active-speed operations accept
1–10. HA interprets media-player volume 0 or fan percentage 0 as power off,
without sending a speed write. Firmware behavior for a direct `S,<id>,0$`
command is not established by the saved evidence.

Clock writes send local `HHMM` and weekday only. Seconds are discarded; no date,
timezone, or DST is sent. Connecting does not sync the clock. Clock changes can
change when existing onboard schedules run.

`ScheduleReport` is a frozen dataclass with `slot`, `start`, `stop`, `days`, and
`speed`. Start and stop are `datetime.time`. The day mask is an integer 0–127:
Monday is bit 0 (`0x01`), Sunday is bit 6 (`0x40`), weekdays are `0x1F`, and
all days are `0x7F`. Schedule speed is 0–10. In the app model, `days=0` disables
and `speed=0` deletes; these semantics are not hardware-verified here.

`set_schedule()` validates all fields before writing. It holds one operation
lock for three ordered writes (start, stop, then days/speed), each expecting an
`Ack`, followed by a slot-matched readback. It compares all fields at minute
precision and returns that report only on a match. It stops at the first
failure. This is not firmware-atomic: failure or cancellation can leave a
partial update. There is no rollback. Save originals and read back before
trying a repair; equal-time, overlap, and overnight-day behavior remain
unverified on hardware.

### Experimental operations: inferred, not proven

The APK constants establish the letters `t` and `I` only. They do not establish
outbound commands, parameters, firmware support, or response schemas. The
library exposes these helpers separately from the evidence-backed operations:

- `get_time() -> bytes`: sends inferred `t,<id>$`. It accepts a raw `T,...$`
  frame using an inferred uppercase response letter. Its field schema is
  unknown, so no time object or weekday is decoded. It may be rejected, echoed,
  or time out. Do not treat the raw frame as an established clock format.
- `set_device_id(new_device_id) -> str`: sends inferred
  `I,<current>,<new>$`, expects an `Ack`, then queries `i$` under the same lock.
  Before attempting the write, it invalidates the cached ID. The ID stays
  unknown until readback succeeds, including after failure or cancellation.
  On a mismatch it retains the actual reported ID and raises `DohmError`;
  on a match it returns the actual ID. Accepted values, persistence, and
  rollback are unknown. This can break HA's observed MAC-to-ID relation and
  leave HA unable to address the device. HA never calls it.

If an experimental ID write leaves `device_id` unknown, a successful
`get_device_id()` (if still connected) or `connect()` can query the actual ID
without needing the old one. Neither proves that the write persisted.

## Deliberate writes only

The following are separate examples for an already connected `client`, not
part of the read-only script. **Do not run them without permission.** Power
and speed affect the machine immediately. Name and clock changes can affect
other controllers or active schedules. A schedule write can overwrite a slot
and can partly succeed. Save originals first; any later restoration is another
non-atomic write, not a rollback guarantee.

```python
from datetime import time

await client.set_name("Bedroom")
await client.set_time(time(21, 33), weekday=2)
original = await client.get_schedule(1)
updated = await client.set_schedule(1, time(22), time(6), days=0x1F, speed=5)
# Separate immediate controls, only when intended:
await client.set_speed(3)
await client.set_power(True)
```

Do not add experimental ID writes to normal setup or automation. No write,
bond clearing, or hardware actuation was performed to prepare this guide.

## Errors and reply limits

- `DohmCommandError` (a `DohmError`): device replied `Failed NN$`.
- `DohmProtocolError` (a `DohmError`): malformed frame or unsupported echo.
- `DohmError`: not connected, unknown ID, recovery conflict, or readback mismatch.
- `DohmRebondUnsupported` (a `DohmError`): backend cannot clear/request a bond.
- `TimeoutError`, validation `TypeError`/`ValueError`, and transport errors can
  also propagate. Cancellation can leave an already sent write applied.

Operations serialize and match reply types; schedule reads also match slots.
Unrelated valid reports are ignored. The wire protocol has **no request IDs**.
A same-type stale report or generic `OK$` (`Ack`) cannot be correlated reliably
with the current request after retries or cancellation. Queue draining, type
matching, and readback reduce mistakes but cannot remove this ambiguity.
Opening commands can be retried on the same link; a timeout does not prove a
write failed or that a bond is bad. Re-read state before retrying a write.

## Pure protocol API and HA boundary

`custom_components.dohm.protocol` encodes bytes without BLE: `query_id`,
`query_power`, `query_speed`, `query_name`, `set_power`, `set_speed`, `set_name`,
`set_time`, `query_schedule`, and `set_schedule` (a tuple of three payloads).
`query_time` and `set_device_id` are experimental inferred encoders.
`device_id_from_address` implements the observed MAC-suffix derivation;
it is not suitable for a macOS host UUID. `parse(payload)` decodes `DeviceId`,
`PowerReport`, `SpeedReport`, `NameReport`, `ScheduleReport`, `Ack`, or `Failure`
and raises `ValueError` for malformed/unknown frames. It does not decode a
clock report.

Home Assistant still exposes only power and speed/volume through the selected
media player or fan. It does not automatically sync names, the clock, or
schedules, and does not change device IDs. Standard GATT firmware, serial, and
battery reads are **not implemented** in this client or the integration.

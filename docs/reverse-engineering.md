# Reverse-engineering runbook

How we captured and decoded the Dohm BLE protocol, written so it can be redone
on a new unit, a new firmware, or a new Mac. Two independent halves: **capture**
(sniff the official app) and **connect** (drive the device from our own code).

For static APK extraction without connecting to hardware, see
[Android app protocol extraction](android-app.md). It confirms the schedule
writers, weekday masks, and clock format missing from the original capture.
For current HA diagnostics, use [troubleshooting](debugging.md) and
[pairing/recovery](pairing.md), not the old experimental tools below.

**Before a live capture or probe:** get permission for any setting change or
new pairing. Stop competing clients and pause HA when another tool will own
the connection. Starting the app, creating a new bond, or holding the button
can replace a working HA pairing. Reconnect HA afterward and check its replies.

## Part A — Capture the app's traffic (PacketLogger on macOS)

We sniffed the official app instead of guessing. On an Apple Silicon Mac you can
run the iPhone app locally, so its Bluetooth goes through the Mac's one radio,
which PacketLogger can tap.

1. **Run the iOS app on the Mac.** Mac App Store → search the app → install from
   the "iPhone & iPad Apps" results. Confirm it actually controls the device.
2. **Install PacketLogger.** It ships in *Additional Tools for Xcode*
   (developer.apple.com/download/all → match your Xcode version) → drag
   `Hardware/PacketLogger.app` to `/Applications`.
3. **GOTCHA — PacketLogger shows nothing by default on recent macOS.** Local HCI
   logging is gated behind Apple's **Bluetooth logging profile**. Without it the
   capture stays empty even though the app works. Fix:
   - Download the **Bluetooth** profile from
     <https://developer.apple.com/bug-reporting/profiles-and-logs/> *directly on
     the Mac* (it is mislabeled "Bluetooth Logging for iOS" but is what unlocks
     macOS capture).
   - Install via **System Settings → General → Device Management**, then
     **reboot**.
   - Verify: open PacketLogger, toggle Bluetooth off/on — you should see a flood
     of HCI packets. No flood ⇒ the profile/log isn't active; don't proceed.
4. **Capture cleanly.** Clear PacketLogger. In the app, disconnect + reconnect
   the device, then do labeled actions a couple seconds apart (set speed 3, power
   off, power on). File → Save a `.pklg`.
   - GOTCHA: PacketLogger's auto-save filenames use a **narrow no-break space**
     (U+202F) before AM/PM. Typing a normal space won't match; use shell globs
     (`*.pklg`) or tab-completion to reference them.

## Part B — Read the transcript (tshark)

`tools/extract_writes.py` turns the `.pklg` into an ordered, UTF-8 dialogue of
ATT writes (app→device) and notifications (device→app):

```
uv run tools/extract_writes.py path/to/capture.pklg
```

- macOS does **not** expose local Bluetooth to Wireshark/tshark for *live*
  capture (no Bluetooth interface in `tshark -D`), but tshark reads saved
  `.pklg` files fine.
- GOTCHA: tshark renders `btatt.value` as **continuous hex** (`4f4b24`), not
  colon-separated; the decoder handles both.
- The writes reference the characteristic by numeric **handle** (e.g. `0x001a`),
  not its UUID, so filter by handle, not UUID.

This is how we found the device-ID-prefix scheme (see `protocol.md`): the bare
`S,03$` we tried in LightBlue failed because real commands embed the device id
(`S,<id>,3$`), which the app learns by sending `i$`.

## Part C — Connect from our own code (bleak on macOS)

Sniffing tells you the protocol; you still must prove a non-app client can drive
the device. `tools/probe.py` does this. Three macOS/CoreBluetooth gotchas, each
of which we hit:

1. **For deliberate initial pairing/recovery only**, hold the top button for
   about five seconds. Do not press it when checking a healthy existing bond.
2. **Connect by a scanned device, not a hardcoded address.** Passing a UUID
   string straight to `BleakClient` is unreliable on CoreBluetooth — scan first
   (`BleakScanner.discover`) and connect to the discovered `BLEDevice`. Match by
   **BLE local name** (`MARPAC_DOHM…`), because:
   - the per-host CoreBluetooth address is **not** portable (the Pi sees a
     different one), and
   - the `AA114B7E-…` value we were first given is the **service UUID**, not a
     peripheral address — connecting by it fails.
3. **Inspect services, not numeric handles.** An early guessed characteristic
   UUID caused `BleakCharacteristicNotFoundError`. The captured and Android
   command UUID is now known: `00005601-d102-11e1-9b23-00025b005aa5`. The old
   probe enumerates GATT and auto-picks a write+notify characteristic; confirm
   it selected that UUID. Numeric ATT handles can differ between captures.

```
uv run tools/probe.py --scan     # list nearby devices, confirm name + RSSI
uv run tools/probe.py            # scan, match MARPAC_DOHM*, connect, dump GATT
```

## Button use and recovery

A healthy existing bond was observed to reconnect without another button press.
That does not guarantee unattended reconnection forever. The button makes the
device discoverable for setup and can also replace a working pairing. Use it
only for deliberate initial pairing or recovery, not every scan/reconnect.
See [current recovery instructions](pairing.md).

## Tool limitations

- `tools/extract_writes.py` analyzes saved captures and does not connect to or
  actuate the device. It requires `tshark`.
- `tools/probe.py` is an interactive macOS-era experiment. It does not use the
  current HA connection/recovery path or explicitly request pairing. It scans
  by name, and an unmatched explicit address can fall back to another named
  device. Confirm the printed address/UUID before sending any command.
- `tools/explore.py` deliberately changes power and speed, uses a short
  1.5-second reply timeout, and probes values including speed zero. Restoration
  is not protected by `finally`; interruption or a failed query can leave
  settings changed. Do not use it as a safe current smoke test.
- The tools' name matching can miss advertisements with no local name. The
  integration has UUID-primary discovery and a name fallback.

Do not run live tools to validate a documentation/code change without permission.

## Discovery matcher (resolved)

The device's advertisement (dumped with `tools/advert.py` while holding the
button) contains **no local name** — `local_name` is `None` — but **does**
advertise the command service UUID:

```
=== MARPAC_DOHMB2 @ ...  (-46 dBm) ===
  local_name:    None
  service_uuids: ['00005600-d102-11e1-9b23-00025b005aa5']
```

This observation is why the **service UUID** `00005600-…` is the primary
matcher. The current `manifest.json` also has a `MARPAC_DOHM*` local-name
matcher, and the config flow uses a name fallback. Name availability varies by
scanner/backend; do not rely on it being present or absent everywhere.

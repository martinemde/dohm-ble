# Reverse-engineering runbook

How we captured and decoded the Dohm BLE protocol, written so it can be redone
on a new unit, a new firmware, or a new Mac. Two independent halves: **capture**
(sniff the official app) and **connect** (drive the device from our own code).

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

1. **Hold the top button ~5s** to make the device connectable before scanning.
2. **Connect by a scanned device, not a hardcoded address.** Passing a UUID
   string straight to `BleakClient` is unreliable on CoreBluetooth — scan first
   (`BleakScanner.discover`) and connect to the discovered `BLEDevice`. Match by
   **BLE local name** (`MARPAC_DOHM…`), because:
   - the per-host CoreBluetooth address is **not** portable (the Pi sees a
     different one), and
   - the `AA114B7E-…` value we were first given is the **service UUID**, not a
     peripheral address — connecting by it fails.
3. **Don't trust a hardcoded characteristic UUID.** `start_notify(<uuid>)` threw
   `BleakCharacteristicNotFoundError`. Instead, enumerate the GATT table after
   connecting and auto-pick the single characteristic whose properties include
   both a write flag and `notify`. The probe prints the full table for the
   record.

```
uv run tools/probe.py --scan     # list nearby devices, confirm name + RSSI
uv run tools/probe.py            # scan, match MARPAC_DOHM*, connect, dump GATT
```

## Button behavior (resolved)

The top-button hold is **only for discovery** — it makes the device advertise so
a new client can find it. Once known, reconnection works **without** the button.
Confirmed by reconnecting from a fresh probe run with no button press.

Implication for Home Assistant: the config flow asks for one button press at
setup (to discover/pair), after which HA reconnects unattended forever. No
recovery problem.

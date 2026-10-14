# Android app protocol extraction

## Artifact and reproducibility

The Google Play link identifies **Marpac Connect**, package `com.marpac`.
The APK was obtained from APKPure, not directly from Google Play:

- Version name **1.0.7.0**, version code **10700** (decoded manifest).
- APK size **6,132,772 bytes**.
- SHA-256 `721e0307d6df5abfdcaeff93faf150f5b7035c9c1a92aab1130588a90fa324c6`.
- Listing: <https://apkpure.com/marpac-connect/com.marpac/download>.
- Download: <https://d.apkpure.net/b/APK/com.marpac?versionCode=10700>.

The hash matches the mirror's published hash. This is **not** independent
verification of Marpac's signing certificate. The APK was not installed or run.
No cloud accounts or services embedded in the app were accessed.

Local files (ignored by git):

```text
artifacts/marpac-1.0.7.0/com.marpac-1.0.7.0.apk
artifacts/marpac-1.0.7.0/provenance.json
artifacts/marpac-1.0.7.0/jadx/sources/com/marpac/
artifacts/marpac-1.0.7.0/jadx/resources/
```

Use JADX **1.5.6** and Java 21 to reproduce the extraction:

```sh
mkdir -p artifacts/marpac-1.0.7.0
curl -fL 'https://d.apkpure.net/b/APK/com.marpac?versionCode=10700' \
  -o artifacts/marpac-1.0.7.0/com.marpac-1.0.7.0.apk
sha256sum artifacts/marpac-1.0.7.0/com.marpac-1.0.7.0.apk
jadx --show-bad-code -d artifacts/marpac-1.0.7.0/jadx \
  artifacts/marpac-1.0.7.0/com.marpac-1.0.7.0.apk
```

JADX completed with exit code 3 and 10 errors across bundled dependencies.
No `JADX ERROR` or `UnsupportedOperationException` placeholders occur in the
74 Java files under `com/marpac`. Critical command builders and notification
parsers decompiled without such errors. Decompiled Java is evidence of **app
behavior**, not proof that every firmware accepts every command.

## Source map

Paths below are relative to `jadx/sources/com/marpac/`.
Line numbers refer to the extraction with the options above.

| Evidence | Source |
|---|---|
| All command letters and separators | `service/BluetoothRequest.java:14–33` |
| Decimal vs hex, padding, time format | `service/BluetoothRequest.java:102–173`, `CommandBuilder` |
| All used command constructions | `model/Device.java:220–291` |
| Service and characteristic UUIDs | `service/BluetoothService.java:104–105` |
| Bond creation | `service/BluetoothService.java:216–234`, `connectToDevice` |
| Notification setup and GATT writes | `service/BluetoothService.java:406–459` |
| Opening ID query and clock sync | `service/BluetoothService.java:533–555`, `onDescriptorWrite` |
| Firmware read before notifications | `service/BluetoothService.java:515–529,571–576` |
| Notification decoding, including schedule | `service/BluetoothService.java:580–640` |
| Power/speed polling | `service/BluetoothService.java:267–276,658–675` |
| Clock weekday | `model/Device.java:256–261`, `sendSetTimeCommand` |
| Schedule delete/disable/defaults | `model/Schedule.java`, `setDefaults`, `setEnabled`, `delete` |
| Day bit ordering | `view/WeekView.java:13–21` |
| Three schedule slots | `controller/activity/DeviceScheduleListActivity.java:23–32` |
| Overnight duration calculation | `controller/activity/DeviceScheduleDetailActivity.java`, `onTimeChanged` |
| Name input restrictions | `controller/activity/firstrun/NameDeviceActivity.java`, input filter |

## Recovered commands

Use the device's reported ID, shown here as `0136C4`. Every command ends in `$`.
The transport UUIDs exactly match those already in this integration.

```text
i$                          # query ID; no ID parameter
m,0136C4$                    # query power
M,0136C4,1$                  # on; 0 means off
s,0136C4$                    # query speed/volume
S,0136C4,03$                 # app writes two decimal digits
n,0136C4$                    # query device name
N,0136C4,Bedroom$            # set device name
T,0136C4,2133,2$             # local 21:33 on Tuesday
p,0136C4,1$                  # read slot 1
P,0136C4,1,1,2030$           # selector 1, slot 1: start 20:30
P,0136C4,2,1,0730$           # selector 2, slot 1: stop 07:30
P,0136C4,3,1,7F,02$          # selector 3, slot 1: all days, volume 2
```

Ordinary speed writes are padded in Android (`03`), unlike the existing
integration's bare decimal (`3`). The latter is already hardware-captured as
working. There is no reason to change it merely to match the Android UI.

### Clock

The app sends local wall-clock time with no date, seconds, timezone, or DST
field. `LocalDate.getDayOfWeek().getValue()` means **Monday=1 … Sunday=7**.
It sends `T` during initial pairing and on reconnect after notifications are
enabled. If HA uses onboard schedules, clock sync must also account for clock
drift and local timezone changes. The current HA media player does not need it.

### Schedules

The app models **three slots (1–3)**. Each update is the three separate commands
above, in that order. The selector comes **before** the slot. A read produces:

```text
P,1,2030,0730,7F,02$
```

Fields are slot, start time, stop time, hex day mask, decimal volume.
The app notification parser confirms this exact order.

| Day | Mask |
|---|---|
| Monday | `01` |
| Tuesday | `02` |
| Wednesday | `04` |
| Thursday | `08` |
| Friday | `10` |
| Saturday | `20` |
| Sunday | `40` |
| Weekdays | `1F` |
| Every day | `7F` |

- **Disabled:** day mask `00`, with a nonzero volume.
- **Deleted:** volume `00`; deletion still writes all three fields.
- **Default:** 22:00–06:00, weekdays (`1F`), volume 5.
- Active volumes in the UI are 1–10; zero is a deletion sentinel, not silence.
- The UI treats start >= stop as ending on the next day. Exact firmware
  behavior for equal times, overlaps, and which day controls an overnight stop
  still needs a hardware check.
- `Schedule.setEnabled()` only toggles masks `00` and `1F`. For a custom mask
  it can silently do nothing. HA should not copy this UI bug; retain a saved
  mask when disabling/re-enabling a custom schedule.
- `createShadowSchedule()` creates a one-minute secondary slot at index+1,
  but **has no caller** in this APK. Do not implement it as a required workaround.
- The list reader requests index+1 even after reading slot 3, so it can send a
  stray `p,<id>,4$`. This is not evidence of a fourth supported slot.

These semantics are recovered from the Android app. Existing captured
schedule traffic corroborates the wire format, but delete/disable operation
has not been tested on hardware here. Slot updates are not atomic; a future
client should serialize the whole three-write sequence and confirm readback.

### Names and app-only settings

The name UI allows letters/digits only, up to **10 characters**. The length
limit comes from `jadx/resources/res/values/integers.xml`
(`device_name_max_length`). These are UI constraints, not established firmware limits. Never send unescaped `,` or
`$` inside a name or ID; the protocol has no escaping scheme.

Default volume is stored in the app's SQLite device record. There is **no
separate default-volume BLE command** in the used builders. Alexa delivery
ultimately calls the same `M`/`S` helpers via `MessagingService`; HA does not
need to reproduce the app's cloud infrastructure. Tone adjustment has no extra
BLE command in this app; the setup screen is guidance for manual adjustment.

### Declared but unproven commands

`COMMAND_GET_TIME = "t"` and `COMMAND_SET_UNIT_NUMBER = "I"` exist as constants,
but neither has an outbound caller. The uppercase `I` is used to recognize
ID replies. This does **not** establish that changing the ID is supported,
or establish a clock-query reply format. No extra guessed commands were added.

## Connection findings and limits

- Android's saved-device `connectToDevice()` calls `createBond()` immediately
  after `connectGatt()`. New-device `discoverDevice()` instead connects with
  `autoConnect=true` and has no explicit `createBond()` call. Its UI declares
  pairing successful only after receiving an ID reply, not after checking a
  bonded-state event. The ID field does not replace BLE pairing/encryption.
- New-device setup reads `i$` and stores the returned identifier. The APK does
  **not** independently prove the integration's MAC-suffix derivation.
- The app reads standard firmware revision `180a/2a26` before enabling
  notifications. It defines `Rev 1.3`/`Rev 1.6` constants, but no used command
  branch depends on the firmware version in this APK.
- The app enables the notification descriptor once per setup. It does not
  confirm or refute the integration's observed need to re-arm it per command
  with BlueZ/ESPHome.
- Android's scheduled poll task runs every **1 second**, asking power and
  speed. This differs from the older iOS capture and HA's 30-second polling.
- Other standard service UUID constants include malformed/nonstandard
  expansions; use standard Bluetooth UUIDs and discovered GATT services, not
  blind copies of every app constant.

## Home Assistant next steps

Pure name/time/schedule encoders and a schedule report decoder can now be
implemented from evidence. They do not by themselves expose new HA entities.
Before adding client operations, match reply types (`Ack`, `NameReport`,
`ScheduleReport`) and serialize multi-write schedule updates. Then check on
hardware: read all three slots, save originals, sync clock, alter one unused
slot with permission, read back, and restore it. Device Information reads can
also correct the README's currently unfulfilled firmware/serial promise.

See the [pairing audit](pairing.md) for the missing explicit pairing request
found in the existing client and the local connection fix. The HA entity
features remain power/volume only. No BLE connection, pairing, timer change,
or hardware actuation was performed in this extraction.

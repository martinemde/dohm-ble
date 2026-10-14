# Pairing audit

This audit compares Marpac Connect 1.0.7.0 with the integration and its actual
Python dependencies. See [APK provenance](android-app.md). It does not test a
physical Dohm or a Home Assistant Bluetooth adapter.

## What the Android app actually does

Two paths differ in `com/marpac/service/BluetoothService.java`:

1. **New device:** `discoverDevice()` (809–821) calls `connectGatt()` with
   `autoConnect=true`. There is no explicit `createBond()` in this method.
2. **Saved device:** `connectToDevice()` (216–234) calls `connectGatt()` with
   `autoConnect=false`, then **`createBond()`** immediately.
3. Both proceed through service discovery, read firmware revision `180a/2a26`,
   enable local notifications, and write the enable value to the notification
   descriptor (515–575 and 406–432).
4. For a new device, after a successful descriptor write, send **`i$`**
   (533–555). Only receipt of **`I,<id>$`** causes `PairedDeviceEvent`
   (611–619). `PairDeviceActivity.pairedDeviceEvent()` stores the ID, sets the
   clock, and shows success. It does not check a `BOND_BONDED` event.
5. For saved devices, after descriptor setup the app sets the clock and drains
   queued commands. The app does not explicitly wait for a bond-state event.

So the APK contains a real bonding request on reconnect, but its notion of
"paired" also includes application setup and a successful protocol reply.
It cannot by itself prove the exact peripheral security requirements.

There is no extra pairing password, proprietary authentication command, or
recovered secret handshake. Device ID is a command address, not a bond key.
Name and clock writes are housekeeping. The app does not rewrite the CCCD for
every command; that observation in the integration docs came from the earlier
iOS capture and local hardware experiments.

## Gap in the integration before this change

`_default_connector()` called `establish_connection()` without `pair=True`.
In the installed **bleak-retry-connector 4.6.1**, that parameter defaults to
**False** and is forwarded to the Bleak client. Installed **Bleak 3.0.2** also
defaults to **False**. Normal setup therefore requested a GATT connection and
notifications, but **did not explicitly request a bond**. Only the deliberate
`rebond()` recovery path called `pair()`.

This does not prove that no bond was ever established. Some systems pair
implicitly when an authenticated attribute is accessed; old stored bonds can
also make the missing request invisible. It does mean pairing was left to
backend/peripheral side effects rather than requested deliberately.

The local fix requests pairing in both normal `establish_connection()` paths,
including service-cache rediscovery. The default recovery connector keeps
`pair=False`, because a stale-key authentication failure must not prevent
opening the connection needed to clear that key. Recovery explicitly calls
`unpair()`, reconnects if unpair removed the link, then calls `pair()`.
Injected test connectors keep their one-argument contract. For BlueZ, normal
connections also call explicit `pair()` to cover already-connected links where
`connect(pair=True)` skips its pairing step. A stored valid key is preserved.

## Backend checks

- **BlueZ / Bleak 3.0.2:** `pair=True` calls D-Bus `Pair` for an unpaired
  device while establishing a new connection. An existing `Paired` state is
  reused, not cleared. An already-connected device can bypass this part of
  `connect()`. Therefore even `pair=True` is not proof of encryption or a
  matching key in every state. The additional normal BlueZ `pair()` call has
  a 30-second timeout and releases its client if pairing fails or is canceled.
- **ESPHome / bleak-esphome 4.1.0:** `connect(pair=True)` awaits `_pair()`
  before service discovery. `pair()` sends a proxy pairing request and raises
  a BLE error if its response says not paired. This package was inspected in
  an isolated research environment, not added to integration dependencies.
- ESPHome explicit pair/unpair require firmware advertising **`PAIRING`**
  (documented as ESPHome 2024.3.0 or newer). Unsupported firmware raises
  `NotImplementedError`; the normal connector translates it to an actionable
  `DohmError` recommending an upgrade. It does not silently skip pairing.
- **CoreBluetooth / macOS:** Bleak ignores `pair=True`; macOS handles pairing
  when protected attributes are accessed. Explicit unpair is unsupported.
- Older HA/bleak-esphome releases may differ. The version deployed on the
  user's HA host/proxy was not inspected here.

## Deliberate stale-bond recovery

Use **Configure → Clear the pairing and pair again** after putting the Dohm in
pairing mode. Ordinary timeouts never trigger this action automatically.

- Wait for an in-flight protocol command, then close the normal connection.
- Open a short-lived recovery connection with `pair=False`. A stale key must
  not prevent reaching the unpair operation.
- Clear that controller's stored bond exactly once.
- **ESPHome:** if the link remains connected, request pairing on that link.
- **BlueZ:** unpair removes the device object and disconnects it. Reconnect
  the same recovery client first, allowing native BlueZ to rediscover the
  removed device path. Retry transient BLE errors up to three attempts within
  one **30-second** reconnect budget. Then explicitly call `pair()`.
- Pairing itself has a **30-second** wait limit. These bounds do not include
  the initial connection or backend unpair operation.
- Always attempt to disconnect the recovery client afterward, including
  cancellation, with a **5-second** transport-cleanup budget. Normal-link
  notification teardown also has its own 5-second budget and always attempts
  transport disconnect even if notification teardown is canceled or hangs.
  A cleanup failure is logged without hiding the original recovery outcome. Then the coordinator rebuilds
  the normal connection and checks power/speed.
- If unpair succeeded but reconnect/pair fails, report **bond cleared, re-pair
  failed** rather than claiming complete recovery. If unpair itself fails,
  propagate that error rather than claiming the key was removed.

This mirrors the app's saved-device sequence at the relevant level: establish
connection, request a bond, then use notifications/commands. Clearing stale
keys is an integration recovery action, not an operation found in the app's
normal reconnect path.

## Moving between Bluetooth proxies

A proxy change is allowed. Bond keys belong to each controller/proxy, not to a
shared Home Assistant key store. A normal new connection requests pairing but
never deletes a stored key. If that proxy has a stale key, use the deliberate
recovery action to clear and re-pair the **currently selected** controller.

HA can select a different controller on each connection based on signal,
connection failures, and free slots. In the audited **HA 2026.2.3 / habluetooth
6.1.0** stack, it ignores the source on the BLEDevice passed to its client and
has no supported source-pin constructor argument. Passing a "fresh device"
is therefore not a controller-pinning guarantee.

After an unpair that disconnects the link, recovery compares the **actual
connected controller** before and after reconnecting. If HA changed it, or
identity is unavailable, stop before asking the replacement controller to pair.
The original key was cleared; no second controller is unpaired automatically.
Retry the recovery action to repair the controller HA now selects. If repeated
route changes prevent completion, temporarily leave only the intended
connectable adapter/proxy available for the operation.

The guard uses two narrow private metadata fields because no public source-pin
API exists in the audited stack: HA's `_connected_scanner.source`, or native
BlueZ's `_backend._device_path` adapter prefix. If those fields stop supplying
identity, disconnected recovery fails closed. This is not a supported pinning
API and needs rechecking when the Bluetooth stack changes. It does not stop
normal handoff, and stable-link ESPHome pairing is unchanged.

No seamless multi-proxy bonding guarantee is claimed. If the observed
single-bond behavior holds, pairing a new controller can invalidate the old
one; moving back may need another deliberate repair. The device's pairing
button may still be required. Neither controller switching nor repeated
silence permits automatic clearing of keys.

## Remaining validation limits

The setup form creates an entry before any Bluetooth exchange. Entity setup
then polls power/speed, so creating an entry is **not** proof of pairing.
Neither the APK nor unit tests establish that the Dohm keeps exactly one bond,
that pressing the button always replaces one, or that notifications require
encryption. Those are existing device observations, not new static-code facts.

The existing policy of **never automatically clearing a bond on silence** is
important. A slow first reply, a wrong derived ID, a disabled CCCD, interference,
and a stale key can all look like a connected device that does not answer.

## Safe hardware confirmation

Do this on the actual controller that HA will use (local adapter or proxy).
Do not erase a working bond just to run the test.

1. Stop the phone app and any other BLE client. The observed device accepts
   one controller connection at a time.
2. If setting up a new controller or deliberately recovering a stale bond,
   hold the top button for about five seconds. Avoid doing this to a healthy
   existing setup merely to collect logs.
3. Enable debug logging for `custom_components.dohm`, `bleak_retry_connector`,
   and the active backend (`bleak` or `bleak_esphome`). For ESPHome, collect its
   logs too, including authentication completion/failure messages.
4. Confirm a successful pairing/authentication event on that backend. For
   BlueZ, `bluetoothctl info <MAC>` can show `Paired`/`Bonded` where supported,
   but those flags alone do not prove the peripheral still accepts the key.
5. Subscribe and send **`i$`**, without power/volume/timer changes. Confirm
   **`I,<id>$`** and compare its ID with the integration's derived MAC suffix.
   Then query `m,<id>$` and `s,<id>$`. Allow the long first-command timeout.
6. Disconnect cleanly, reconnect **without another button press**, and repeat
   read-only queries. This is the important persistence test, not merely
   observing an ACK for a write or a successful GATT connection.
7. If only reads fail, inspect authentication and notification logs before
   clearing anything. Use the deliberate recovery action only when needed.

No hardware was connected, actuated, paired, or unpaired during this audit.
The local pairing change is unit-tested, not yet proven on a live controller.

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

## Normal connections: restore the 0.3.0 behavior

Version **0.4.2** restores the normal connection policy from **0.3.0**:
`_default_connector()` calls `establish_connection()` without a `pair` argument
and never explicitly calls `pair()`. The installed bleak-retry-connector 4.6.1
and Bleak 3.0.2 default to `pair=False`. Service-cache rediscovery and recovery
from a stuck notification subscription use the same connector.

The **0.4.0/0.4.1 regression** came from interpreting Android's saved-device
`createBond()` call as a requirement for every connection. Those releases
passed `pair=True` and additionally awaited `pair()` on BlueZ. An explicit
pairing error could then prevent an already-configured device from reaching
notification setup or power/speed queries. Two previously configured devices
were reported offline after upgrading, while 0.3.0 worked. Their backend logs
were not inspected, so this mechanism remains a code-based diagnosis, not a
confirmed hardware trace.

Android issues a bonding request but does not wait for a bond-state event
before proceeding. That is not proof that every Bleak backend must complete
an explicit pairing operation before it can use a working link. The fix does
not discard the APK's command/protocol findings or the guarded recovery path.

Normal connections leave stored keys alone and rely on the backend's existing
bond/authentication behavior, as 0.3.0 did. They do not promise a new bond or
that authentication succeeds. Explicit `unpair()` and `pair()` remain confined
to the user's deliberate `rebond()` recovery action. No bond is automatically
cleared after silence or connection failure.

## Backend checks

- **BlueZ / Bleak 3.0.2:** `pair=True` can introduce a D-Bus `Pair` operation
  before normal GATT setup. `pair()` can return early for a device already
  marked paired; it does not prove the peripheral accepts the stored key.
  Normal connections no longer request either operation explicitly. Deliberate
  recovery still reconnects after BlueZ unpair removes the device object.
- **ESPHome / bleak-esphome 4.1.0:** `connect(pair=True)` awaits `_pair()`
  before service discovery. Explicit pair/unpair need firmware advertising
  **`PAIRING`** (documented as ESPHome 2024.3.0 or newer). That capability is
  needed for deliberate bond recovery, not a new requirement for ordinary
  connections. This package was inspected in a separate research environment.
- **CoreBluetooth / macOS:** Bleak handles pairing implicitly when protected
  attributes are accessed. Explicit unpair is unsupported.
- The versions deployed on the reporting HA host/proxies were not inspected.
  Backend behavior can differ across versions.

## Deliberate stale-bond recovery

Use **Configure → Clear the pairing and pair again** after putting the Dohm in
pairing mode. Ordinary timeouts never trigger this action automatically.

- Wait for an in-flight protocol command, then close the normal connection.
- Open a short-lived recovery connection without requesting pairing. A stale
  key must not prevent reaching the unpair operation.
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

This is deliberate recovery, not the normal reconnect sequence. Clearing stale
keys is an integration recovery action, not an operation found in the app's
normal reconnect path.

## Moving between Bluetooth proxies

A proxy change is allowed. Bond keys belong to each controller/proxy, not to a
shared Home Assistant key store. A normal connection does not explicitly
request pairing or delete a stored key. A different proxy may not have a
usable bond. If that proxy has a stale key, use the deliberate
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

No hardware was connected, actuated, paired, or unpaired during this audit or
the 0.4.2 patch. Pairing-free normal reconnects (including two configured
clients), cache rediscovery, notification recovery, and explicit bond recovery
are unit-tested. The patch still needs confirmation on the affected hardware.

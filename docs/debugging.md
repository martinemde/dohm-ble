# Troubleshooting the Dohm integration

This guide describes the current code. Check the version installed in Home
Assistant before assuming it includes a recent fix. Older incident reports
and host-specific commands are in [historical notes](history/debugging-notes.md).
Use the [pairing guide](pairing.md) for the exact recovery sequence and backend
limits, and [protocol reference](protocol.md) for command formats.

## Start here

1. **Check the existing entry.** In Settings → Devices & Services, show disabled
   entries. Re-enable the intended entry before trying to add it again. An
   `already_configured` error is not a pairing failure. Deleting/re-adding an
   entry loses its settings and does not clear a Bluetooth controller's bond.
2. **Check reachability and contenders.** Confirm device power, range, and the
   active Bluetooth adapter/proxy. Close the phone app and other BLE clients.
   Do not press the top button or clear a key merely to collect logs.
3. **Allow startup time.** The first command can wait 30 seconds, up to three
   attempts on the same link. Connection setup adds more time. A single slow
   poll is not proof of a stale key; HA keeps retrying ordinary failures.
4. **Inspect logs.** Distinguish connection failure, authentication failure,
   notification setup failure, and a command that was ACKed but never replied.
   Silence can also mean a wrong device ID or notification state, not just a
   stale bond.
5. **Recover deliberately if needed.** Hold the top button for about five
   seconds, then use Configure → Clear the pairing and pair again. This clears
   a controller key and can replace a working pairing. Do not use it as a
   routine reconnect step. If it reports partial completion, follow the outcome
   and [proxy-change instructions](pairing.md#moving-between-bluetooth-proxies).

## Power or volume snaps back after a command

Control commands publish their acknowledged power/speed without an immediate
readback. A read immediately after an ACK can still report the previous state.
The normal **30-second** polling interval confirms the device state instead;
physical control changes can take until that poll to appear. Polls and commands
are serialized so a poll cannot read old power, pause for a command, then publish
that old power after the command's new state.

This is covered by coordinator tests through the media-player entity, including
an old immediate power report and a poll in progress during turn-on. It has not
been verified on hardware. If the state is still wrong at the next normal poll,
collect the `M,...` command/`m,...` query replies with debug logging below. The
wire protocol cannot distinguish late same-type replies by request ID.

## Enable debug logging

In `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.dohm: debug
    bleak_retry_connector: debug
```

Add `bleak` or `bleak_esphome` logging for the backend in use. For proxies,
collect ESPHome logs too, especially authentication completion/failure events.
Disable extra logging after reproducing the problem.

Useful client messages:

- `pairing recovery selected controller ...`: the actual recovery controller,
  not simply the source on a cached device object.
- `Bluetooth controller changed during recovery`: the old key was cleared,
  but recovery stopped before pairing the replacement controller. Retry on the
  newly selected controller; no second key is cleared automatically.
- `no CCCD exposed ...; re-arm skipped`: the per-command notification workaround
  cannot run on that backend. This is a diagnostic, not proof of a stale bond.
- `ack ...s, reply ...s`: a quick ATT ACK confirms a write was accepted by the
  transport, not that the device processed the command or the bond is valid.
- `cleanup ... failed`: inspect this when a later connection reports
  `Notify acquired` or runs out of connection slots.

## Read-only checks on a local BlueZ controller

Replace `HA_HOST` and `DOHM_MAC` with your installation's values. SSH access and
`bluetoothctl` depend on your HA host/add-on; these commands are not proxy
management commands.

```sh
ssh HA_HOST 'bluetoothctl info DOHM_MAC'
ssh HA_HOST 'ha core logs'
```

`Connected`, `Paired`, or `Bonded` flags are useful but do not prove the device
still accepts the key. `Device ... not available` means this adapter lacks a
usable cached device; it does not by itself prove dormancy. A missing scan can
also mean range, power, scanning, or routing problems. Logs from a local adapter
do not establish the bond state of an ESPHome proxy.

HA CLI logs can have a short buffer. Collect them immediately after reproducing
an error, or use Settings → System → Logs. Do not assume a historical host,
SSH key, MAC, or numeric ATT handle applies to your installation.

## Verify an existing bond without changing device settings

Follow [safe hardware confirmation](pairing.md#safe-hardware-confirmation) on
HA's actual controller. If using another BLE tool, pause the integration and
close competing clients first; resume it afterward. Limit protocol writes to
`i$`, `m,<id>$`, and `s,<id>$`. Disconnect cleanly when done. Querying sends GATT
writes but does not set power, volume, names, clocks, or schedules.

Receiving valid replies, then reconnecting and receiving replies **without
another button press**, is stronger evidence than a connection or write ACK.
Do not test set commands, pairing, unpairing, or scheduling without permission.
`tools/probe.py` and `tools/explore.py` are old experiments, not the HA recovery
implementation; see [their limitations](reverse-engineering.md#tool-limitations).

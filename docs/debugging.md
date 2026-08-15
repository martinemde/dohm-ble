# Debugging the Dohm BLE integration

Working notes for diagnosing connection/notify problems against the real
device, written for a fresh session to resume without re-discovering everything.
Pairs with [protocol.md](protocol.md) and [reverse-engineering.md](reverse-engineering.md).

---

## Notify-acquired leak — root cause found, fixed in v0.1.5

**Symptom:** adding the config entry fails during setup with:

```
Yogasleep Dohm Connect (00:22:A3:01:36:C4)
Failed setup, will retry: error talking to Dohm:
[org.bluez.Error.NotPermitted] Notify acquired
```

That error comes from `client.connect()` → `start_notify()`: BlueZ refuses a
second `StartNotify` because a notify subscription is **already acquired** on
characteristic `00005601` from an earlier connection that never released it.

**Root cause (the one we kept chasing downstream):** `connect()` leaked a
connected, *notifying* client whenever a step **after** `start_notify` failed.
The sequence is `establish_connection` → `_subscribe()` (start_notify, sets
`_notifying=True`) → `identify()` (writes the first command and waits ≤5 s for a
reply). On this **racy, single-connection** link the reply can time out / the
write can abort, so `identify()` raises — and the old `connect()` propagated that
with the client still connected and notify still acquired. Nothing called
`disconnect()`. During setup that became `ConfigEntryNotReady` ("will retry");
the coordinator/client were dropped but the **link stayed up** (held by the
orphaned client), so BlueZ never freed the notify FD. The next setup attempt's
`start_notify` then hit `NotPermitted: Notify acquired`.

Key realization that pins it down: BlueZ tears notify state down with the link,
so a notify subscription can only stay "acquired" while the **link** stays up —
and the link only stays up because *we* leaked a client holding it. That also
explains why v0.1.4's reconnect-self-heal didn't help: the orphaned client keeps
the (ref-counted, HA-managed) connection slot alive, so the new client's
`disconnect()` can't force the physical link down, and `notify_io` never
releases. The cure is to **not orphan the client**, not to recover after.

**Fix (v0.1.5):** `connect()` now wraps everything after the successful
`establish_connection` in `try/except BaseException: await self.disconnect()`
(best-effort; cleanup errors don't mask the real cause). Any partial connect now
releases `stop_notify` + drops the link before propagating, so no orphaned
acquire survives. Covered by `test_connect_cleans_up_when_a_later_step_fails`
and `test_connect_cleanup_failure_preserves_original_error`.

**Defense in depth still in place:** v0.1.3's `stop_notify` before graceful
`disconnect`, and v0.1.4's in-`_subscribe` self-heal (kept as a fallback for
acquires leaked by something *outside* this code path, e.g. a hard HA crash).

**Still to verify on the live device** (couldn't run here — host SSH key needs an
interactive 1Password tap, see below): deploy 0.1.5, then add the config entry a
few times in a row (the racy path) and confirm no `Notify acquired` recurs. If it
*still* recurs after 0.1.5, the remaining suspect is an acquire held by **another
process** (the phone/Marpac app on the single slot), which our code can't reach —
those earlier "candidate fixes" (BlueZ-level `RemoveDevice`/adapter reset; forcing
`StartNotify` over `AcquireNotify`) become relevant only then.

**Immediate manual unblock if stuck pre-0.1.5:** reload the HA **Bluetooth**
integration (Settings → Devices & Services → Bluetooth → ⋮ → Reload) or restart
HA — releases the stuck acquire so a fresh add succeeds.

---

## Environment & access

- **HA host:** `homeassistant.local` (HAOS on a Raspberry Pi, aarch64). SSH user
  is the same as the local Mac user. Reached via the SSH add-on; `/config` is
  mounted.
- **SSH key gotcha:** the key lives in the **1Password SSH agent**. `ssh-add -l`
  (list) works headless, but *signing* needs interactive approval — a background
  / non-TTY session gets `communication with agent failed`. Run SSH with the
  user present to tap the 1Password prompt, or use a dedicated on-disk key.
- **BT adapter:** `D8:3A:DD:69:E3:FE` (hci0).
- **The Dohm:** MAC `00:22:A3:01:36:C4` (TI OUI `00:22:A3`), service UUID
  `00005600-d102-11e1-9b23-00025b005aa5`, command/notify char
  `00005601-…` = ATT handle `0x001a`, its CCCD = handle `0x001b`.
  Device id `0136C4` = lower three MAC bytes.
- **Tools on host:** `bluetoothctl` (5.85), `python3`, `ha` CLI. **Not present:**
  `btmon`, `hcitool`, `gatttool`, `bleak`/`dbus_fast`. `tshark` is on the **Mac**
  (Wireshark 4.6) for capture analysis.

---

## Device behavior (established by live testing — see also memory/protocol.md)

- **Single connection** — one central at a time; the phone app or HA's own stack
  will contend for the slot.
- **Dormant when disconnected.** It does NOT advertise continuously. After ~1h
  with no connection it stops advertising → `connect` returns
  `Device … not available` and a scan finds nothing. Wake it with the top button
  (~5s hold) or by opening the Marpac app. Once **connected**, the link is stable
  (held 30s idle, fan on, no drop).
- **Reads are request/response.** No unsolicited stream, even while running
  (30s subscribed + fan on = zero frames). The official app's "every 5s" updates
  are just it **polling** `m` then `s`. Confirmed in `captures/capture-0900.pklg`.
- **Connect is racy:** first attempt often `le-connection-abort-by-local`
  (contention for the single slot); a retry usually succeeds.

### Protocol (handle `0x001a`, UTF-8, `$`-terminated)

| Direction | Bytes | Meaning |
|-----------|-------|---------|
| → | `i$` | query id → `I,<id>$` |
| → | `m,<id>$` | query power → `M,0$` / `M,1$` |
| → | `s,<id>$` | query speed → `S,NN$` (01–10) |
| → | `M,<id>,1$` / `M,<id>,0$` | power on/off → `OK$` |
| → | `S,<id>,N$` | set speed 1–10 → `OK$` (or `Failed 03$` if out of range) |
| → | `T,<id>,<HHMM>,<DOW>$` | clock-sync (app sends on connect; **optional** for control; for the onboard scheduler) |
| ← | `OK$` / `Failed NN$` | set ack |

The app re-writes the CCCD enable (`01 00` → handle `0x001b`) **before every
command** — a notify re-arm workaround worth mirroring if replies go missing.

---

## Debugging recipes (run on `homeassistant.local`)

### Is it advertising / connected right now?

```bash
ssh homeassistant.local 'bluetoothctl info 00:22:A3:01:36:C4 | grep -E "Connected|RSSI|not available"'
```

`not available` = dormant (needs waking). A scan repopulates BlueZ's cache:

```bash
ssh homeassistant.local '( echo "scan on"; sleep 12; echo "scan off"; echo quit ) | bluetoothctl >/dev/null 2>&1; bluetoothctl info 00:22:A3:01:36:C4 | grep -E "RSSI|Connected|not available"'
```

If a scan finds **zero** packets while other devices show RSSI, it's genuinely
dormant — wake it physically before any connect test.

### Connect, subscribe, send commands, decode replies

Drives `bluetoothctl`'s GATT menu via a timed subshell and pipes the output
through a Python decoder (timestamps each line, prints notification frames as
bytes, strips ANSI/prompt noise). Bytes for a command = ASCII hex, e.g.
`m,0136C4$` → `0x6d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x24`.

```bash
ssh homeassistant.local 'bash -s' <<'OUTER'
bluetoothctl disconnect 00:22:A3:01:36:C4 >/dev/null 2>&1; sleep 2
(
  echo "connect 00:22:A3:01:36:C4"; sleep 5      # retry connect if it aborts:
  echo "connect 00:22:A3:01:36:C4"; sleep 5      # device is racy/single-conn
  echo "menu gatt"; sleep 1
  echo "select-attribute 00005601-d102-11e1-9b23-00025b005aa5"; sleep 1
  echo "notify on"; sleep 1
  echo 'write "0x6d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x24"'; sleep 3   # m,id$  (query power)
  echo 'write "0x73 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x24"'; sleep 3   # s,id$  (query speed)
  # set speed 2:  S,id,2$
  echo 'write "0x53 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x2c 0x32 0x24"'; sleep 3
  # power off:    M,id,0$   (power on = last byte 0x31)
  echo 'write "0x4d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x2c 0x30 0x24"'; sleep 3
  echo "notify off"; sleep 1; echo "back"; sleep 1
  echo "disconnect 00:22:A3:01:36:C4"; sleep 3; echo "quit"
) | bluetoothctl 2>&1 | python3 -u -c '
import sys,time,re
ansi=re.compile(r"\x1b\[[0-9;]*m"); hexln=re.compile(r"^([0-9a-fA-F]{2} )+")
pv=False
for line in sys.stdin:
    s=ansi.sub("",line); s=re.sub(r"\[[^\]]*\]> ","",s).strip()
    if not s: continue
    ts=time.strftime("%H:%M:%S")
    if s.startswith("##"): print(ts,s); continue
    if "Value:" in s:
        pv = ("Attribute" in s and "ManufacturerData" not in s); continue
    if pv and hexln.match(s):
        pv=False; print(ts,"FRAME:",repr(bytes(int(x,16) for x in s.split()[:64] if len(x)==2))); continue
    pv=False
    if any(k in s for k in ("Connection successful","Connected: no","Connected: yes","Notifying","Failed","abort","not available")):
        print(ts,s)
'
echo "POST: $(bluetoothctl info 00:22:A3:01:36:C4 | grep -m1 Connected)"
OUTER
```

Always end with `notify off` + `disconnect` so you don't leave the single slot
held (which blocks HA). **Don't send `S`/`M` *set* commands without the user's OK
— they actuate the physical fan.**

#### Command byte reference (id `0136C4`)

| Command | Bytes |
|---------|-------|
| `i$` | `0x69 0x24` |
| `m,0136C4$` | `0x6d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x24` |
| `s,0136C4$` | `0x73 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x24` |
| `M,0136C4,1$` (on) | `0x4d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x2c 0x31 0x24` |
| `M,0136C4,0$` (off) | `0x4d 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x2c 0x30 0x24` |
| `S,0136C4,N$` | `0x53 0x2c 0x30 0x31 0x33 0x36 0x43 0x34 0x2c 0x3N 0x24` (speed 10 = `0x31 0x30`) |

### Decode a PacketLogger capture (on the Mac)

```bash
python3 tools/extract_writes.py captures/capture-0900.pklg          # printable dialogue
python3 tools/extract_writes.py captures/capture-0900.pklg --all    # incl. CCCD / binary
```

### Check the deployed integration & HA logs

```bash
ssh homeassistant.local 'grep "\"version\"" /config/custom_components/dohm/manifest.json'
ssh homeassistant.local 'grep -n "stop_notify\|_subscribe\|_is_notify_acquired" /config/custom_components/dohm/client.py'
ssh homeassistant.local 'ha core logs 2>&1 | grep -aiE "dohm|notify acquired|notpermitted|abort-by-local|UpdateFailed"'
```

Note: `ha core logs` has a short buffer and is often empty (esp. after a
restart). Catch errors right after triggering a setup retry. `/config` also has
`home-assistant.log.fault` (crash log) when one exists.

---

## What we concluded about the design

- The original **persistent-connection** model is correct. The device dorms only
  when fully disconnected, so on-demand can't work (nothing to connect to at
  8 pm). Keep one connection; rely on `bleak-retry-connector` for the racy
  reconnect; release notify cleanly on every teardown.
- Reads should stay **query-based** (work whether on or off). Match replies by
  **expected type** (power→`M`, speed→`S`, set→`OK`/`Failed`).
- Real-world usage: HA controls on@8pm / off@7am, otherwise hands-off; the phone
  app is the only other contender for the slot.

## Version history

- **v0.1.2** — recover from partial service cache (clear_cache + rediscover).
- **v0.1.3** — `stop_notify` before graceful `disconnect`.
- **v0.1.4** — self-heal a stuck `Notify acquired` on connect (reconnect+retry).
  Insufficient on its own — the leak it tried to recover from was still being
  *created* by `connect()` (see below).
- **v0.1.5** — root-cause fix: `connect()` releases notify + disconnects if any
  step after `establish_connection` fails, so a racy/timed-out `identify()` no
  longer orphans a connected, notifying client. See the section at the top.
- **v0.1.6** — re-arm the CCCD before every command (see below). Mirrors the
  official app; fixes command replies silently going missing on a long-lived
  link.
- **v0.1.7** — drop the link when a command goes unanswered, so a dead-but-
  "connected" client can no longer wedge the integration forever; plus debug
  logging (see below).
- **v0.1.8** — clear a stale bond and re-pair after 3 consecutive failed polls,
  and raise an HA repair issue telling the user to hold the top button. See
  below.

---

## The integration could never recover a wedged link (v0.1.7)

**Symptom (reported 2026-07-28):** after a day or two of working, the fan goes
unavailable and *never* comes back on its own — only a manual reload/re-add
fixes it. The iOS app, by contrast, is slow to resume after a long idle but
always gets there.

**Evidence from the HA host** (recorder DB, `fan.marpac_dohmb2`, metadata_id
5703): last healthy `on` at 2026-07-21 17:06 local, `unavailable` at 07-23
10:28 and 19:47, then entity+device deleted 07-24 00:20. Nothing since. The
`dohm` config entry still exists but has no entity in the registry, so it never
got past `async_config_entry_first_refresh()`. Meanwhile the device *was*
advertising (host adapter saw RSSI −63), so "out of range" was not the blocker.

**Root cause of the non-recovery** (distinct from whatever first breaks the
link): `DohmCoordinator._ensure_connected()` reconnects only when
`client.is_connected` is False, and *nothing* ever called `disconnect()` on a
command failure. Both of this device's known failure modes leave `is_connected`
reporting True — a notify-deaf device still acks writes, and a half-open link
still looks up — so every 30 s poll retried the same dead client forever. No
amount of fixing the *trigger* could restore service, which is why v0.1.3–v0.1.6
each fixed something real and the integration still wedged.

**Fix (v0.1.7):** `_command()` drops the link (best-effort `disconnect()`, which
releases notify first) on any command failure, then re-raises. The next poll
finds `is_connected` False and rebuilds from scratch — the app's behavior.
`DohmCommandError` (`Failed NN$`) is deliberately *not* a teardown: that is a
healthy, answering link refusing one command. Covered by
`test_unanswered_command_drops_the_link`,
`test_transport_error_during_a_command_drops_the_link`, and
`test_rejected_command_keeps_the_link`.

**Note:** this makes the integration self-healing but does **not** explain why
the link goes bad after ~1–2 days. That is still open — hence the logging below.

### Debug logging (v0.1.7)

Enable in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.dohm: debug
```

What each line is for:

- `i$ -> I,0136C4$ (ack 0.03s, reply 4.87s)` — the **ack/reply split** is the
  key measurement. A fast ack with no reply = notify-deaf. It also settles
  whether the ~5 s BlueZ notification cadence (the rationale for raising
  `COMMAND_TIMEOUT` to 15 s) is real on the deployed proxy path.
- `no CCCD exposed for 00005601-…; re-arm skipped` — if this appears, v0.1.6's
  re-arm is a **no-op on this backend** and cannot be what keeps replies
  flowing. Only ever confirmed working on BlueZ; CoreBluetooth hides the CCCD.
- `… failed after 15.00s (TimeoutError: ); dropping the link …` — the new
  teardown firing.
- `reconnecting to 00:22:A3:01:36:C4 via … (rssi …)` — a rebuild attempt, with
  the signal level HA saw.

---

## A desynced bond was unrecoverable — clear it and re-pair (v0.1.8)

**Symptom (2026-08-14):** the integration could not be re-added at all, and
before that it had stopped working entirely. Two separate problems stacked.

**The re-add blocker (not a BLE problem):** the `dohm` config entry still existed
in `/config/.storage/core.config_entries` with `"disabled_by": "user"`.
`config_flow.py:46` and `:72` both call `_abort_if_unique_id_configured()`, which
matches on unique_id regardless of whether the existing entry is disabled — so
every add attempt, discovered *or* manual, aborted `already_configured` before
touching Bluetooth. Disabled entries are hidden in the UI unless you toggle "show
disabled", so the thing doing the blocking is invisible. **Check this first when
an add silently refuses:**

```bash
ssh homeassistant.local 'python3 -c "
import json
d=json.load(open(\"/config/.storage/core.config_entries\"))
print([(e[\"title\"],e[\"disabled_by\"]) for e in d[\"data\"][\"entries\"] if e[\"domain\"]==\"dohm\"])"'
```

**The underlying failure — bond desync.** The Dohm only emits notifications over
an encrypted link, and it stores exactly **one** bond. Its top button grants a
*new* bond, evicting the old one. So troubleshooting by holding the button
actively destroys the pairing the proxy is using. Once that happens:

- `ble_client_base.cpp` → `ESP_GAP_BLE_AUTH_CMPL_EVT` with `success == false`
  calls `log_error_("auth fail reason", ...)` **and nothing else**. ESPHome never
  calls `esp_ble_remove_bond_device`, so the stale LTK is retried forever.
- **`esphome run` / OTA preserves NVS**, so reflashing the proxy does *not*
  clear it. That ritual was always a no-op.

Structurally the same bug as v0.1.7 — retrying a dead thing forever because
nothing tears it down — one layer lower, in someone else's C++. Worth filing
upstream; not Dohm-specific.

**The lever:** `bluetooth_proxy.cpp:246` handles
`BLUETOOTH_DEVICE_REQUEST_TYPE_UNPAIR` by calling
`esp_ble_remove_bond_device(address)`, per-MAC. `bleak_esphome`'s
`ESPHomeClient` exposes it as `unpair()` (with `pair()` alongside). Both need the
`PAIRING` feature flag (ESPHome ≥ 2024.3.0) and both require an active
connection — fine here, since connect *succeeds*; only encryption/notify fails.

**Fix (v0.1.8):** `DohmClient.rebond()` opens its own short-lived connection,
calls `unpair()` then `pair()`, and drops the link so the next poll rebuilds
normally. `DohmCoordinator` counts consecutive failed polls and escalates once
per outage at `REBOND_AFTER_FAILURES = 3` (90s), then raises a `stale_bond`
repair issue telling the user to hold the top button — deleted again on the next
successful poll, so a self-healed outage stays silent. "Not in range" is
deliberately excluded from the count: the device dorms after ~1h idle and that
says nothing about the bond.

Covered by `test_rebond_clears_the_stored_bond_then_pairs`,
`test_rebond_drops_its_own_link`,
`test_rebond_reports_a_failed_repair_without_raising`,
`test_rebond_unsupported_when_the_backend_has_no_unpair`,
`test_rebond_unsupported_when_unpair_is_not_implemented`, and
`test_rebond_refreshes_the_ble_device`.

**Still to verify on the live device — the open question:** whether `pair()`
succeeds when the Dohm is **not** in pairing mode. If it only grants bonds with
the button held, `rebond()` can never be fully hands-off and the repair issue is
the real feature. `rebond()` returns its outcome as a string and the coordinator
logs it at WARNING for exactly this reason — the first real escalation answers it.

**Known limitation:** escalation only fires for a *running* integration. If setup
itself fails, HA retries `async_setup_entry`, which builds a fresh coordinator
each time, so `_failures` resets to 0 and the threshold is never reached.
Recovering a Dohm that won't set up at all is still manual (button + re-add).

**Manual equivalent, if you need it before v0.1.8 escalates:** clearing one bond
no longer needs `esptool.py erase_flash` — that was overkill. The targeted call
is the proxy's `UNPAIR` request for `00:22:A3:01:36:C4`.

---

## Notify goes deaf on a long-lived link — re-arm the CCCD per command (v0.1.6)

**Live-confirmed from the Mac (CoreBluetooth) on 2026-06-03.** Driving the real
device standalone (`tools`-style bleak script): connect + `start_notify`
succeeded, then the first `i$` query got **no notification reply** across
retries — yet every `write_gatt_char(response=True)` was **ATT-acked** by the
device. So the link was fine both ways; the device had simply **stopped emitting
notifications**. After re-arming the subscription (on CoreBluetooth, a
`stop_notify`/`start_notify` toggle — the only way to rewrite the hidden CCCD),
replies immediately resumed: `M,1$`, `S,02$`, and set `OK$` all came back.

This confirms the long-standing note: **the official app rewrites `01 00` to the
CCCD (handle `0x001b`) before every command.** The Dohm's TI module drops its
notify arming on an idle/long-lived link, so a persistent-connection poller (HA)
eventually sees `get_power`/`get_speed` time out even though the connection is up
and writes are acked — which the coordinator surfaces as `UpdateFailed`.

**Fix (v0.1.6):** `DohmClient._command()` now calls `_rearm_notify()` first —
it writes `01 00` to the command characteristic's CCCD (matched by UUID
`00002902-…`) before each write. Best-effort: backends that hide the CCCD
(CoreBluetooth) return no descriptor and we fall back to the existing
`start_notify` subscription. Covered by
`test_command_rearms_cccd_before_each_command`,
`test_command_succeeds_when_device_needs_rearm_each_time`,
`test_command_tolerates_missing_cccd`, and
`test_command_survives_rearm_write_failure`.

**Note on HA backend:** on BlueZ (and ESPHome proxies) the CCCD *is* exposed, so
the integration can rewrite it directly like the app — no `stop/start` toggle
needed (which on BlueZ risks re-tripping `Notify acquired`).

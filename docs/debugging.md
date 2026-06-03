# Debugging the Dohm BLE integration

Working notes for diagnosing connection/notify problems against the real
device, written for a fresh session to resume without re-discovering everything.
Pairs with [protocol.md](protocol.md) and [reverse-engineering.md](reverse-engineering.md).

---

## ⚠️ CURRENT BUG (unresolved as of v0.1.4)

**Symptom:** adding the config entry fails during setup with:

```
Yogasleep Dohm Connect (00:22:A3:01:36:C4)
Failed setup, will retry: error talking to Dohm:
[org.bluez.Error.NotPermitted] Notify acquired
```

That error comes from `client.connect()` → `start_notify()`: BlueZ refuses a
second `StartNotify` because a notify subscription is **already acquired** on
characteristic `00005601` from an earlier connection that never released it.

**What we've shipped so far (and why it's not enough):**

- **v0.1.3** — `disconnect()` calls `stop_notify()` before dropping the link.
  Fixes the *graceful* teardown path only. Does nothing for a subscription
  already stuck from a prior **unclean** disconnect (crash, dropped link,
  integration reload), which is the state the adapter gets into.
- **v0.1.4** — `connect()`/`_subscribe()` catches the `Notify acquired`
  `BleakError` and self-heals: `stop_notify` → full `disconnect()` → reconnect →
  retry `start_notify` once. **Still reported failing** by the user.

**Leading hypotheses for why v0.1.4 isn't sufficient (investigate these):**

1. **Process-held leaked FD.** `bleak`/BlueZ delivers notifications over an
   `AcquireNotify` file descriptor. If a *previous* `BleakClient` object inside
   HA's long-running process leaked without closing that FD, BlueZ keeps the
   subscription "acquired" regardless of our new client reconnecting. Only the
   holding process closing the FD (HA restart) or a BlueZ-level reset releases
   it. Our in-`connect()` reconnect can't reach that other client's FD.
2. **Recovery reconnect contends and fails.** Connecting is racy on this device
   (`le-connection-abort-by-local`, see below) because it's single-connection
   and HA's own stack/the phone app fight for the slot. The recovery's
   `disconnect()`+reconnect may itself get aborted, so the retry `start_notify`
   never runs cleanly.
3. **HA Bluetooth connection pooling** keeps the device connected across setup
   retries, so our `disconnect()` doesn't actually drop the underlying
   connection, so BlueZ never releases the per-connection notify state.

**Candidate next fixes (not yet tried):**

- Register a **`disconnected_callback`** (bleak-retry-connector's
  `establish_connection` accepts one) so `stop_notify` also runs on *unexpected*
  drops, not just graceful `disconnect()` — prevents the leak at the source.
- **Proactively `stop_notify` before `start_notify`** on every connect, to clear
  leftover state from this process's prior client.
- A **BlueZ-level reset** when stuck: `bluetoothctl` `RemoveDevice` (then
  rediscover) or adapter power-cycle. Heavy (disrupts all BLE) — last resort.
- Confirm whether bleak is using `AcquireNotify` vs `StartNotify` here and
  whether forcing `StartNotify` (CCCD-only) avoids the FD-leak class entirely.

**Immediate manual unblock:** reload the HA **Bluetooth** integration (Settings →
Devices & Services → Bluetooth → ⋮ → Reload) or restart HA — releases the stuck
acquire so a fresh add succeeds (until it recurs).

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
  **Still failing — see CURRENT BUG above.**

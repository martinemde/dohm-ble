# Marpac Dohm BLE protocol

Reverse-engineered from a PacketLogger capture of the official Dohm macOS app
(`captures/capture-2133.pklg`, 2026-06-02), then checked against the Android
Marpac Connect 1.0.7.0 APK. See [Android extraction and source references](android-app.md).
App-derived details below are not a substitute for hardware verification.

## Transport

- Command service `00005600-d102-11e1-9b23-00025b005aa5` containing a single
  characteristic `00005601-d102-11e1-9b23-00025b005aa5`. Numeric ATT handles
  vary by capture/unit; discover them rather than hardcoding an old handle.
  Writable **with response**, **notify**, **not readable** — state arrives only
  via notifications.
- Also present: Device Information Service `180a` (model/serial/firmware) and
  Battery Service `180f` with battery level `2a19` `[read,notify]`. Standard GATT
  firmware, serial, and battery reads are not implemented by the standalone
  client or HA integration.
- `aa114b7e-…` (originally mistaken for the characteristic) appears only in the
  advertisement, not the GATT table.
- All payloads are UTF-8 strings terminated by `$`.

## Reply taxonomy

Four kinds of notification come back:

- valid **set** → `OK$`
- valid **query** → a typed report (`I,…$`, `S,0N$`, `M,1$`, `N,…$`)
- **out-of-range value** → `Failed NN$` (space, not comma; e.g. `Failed 03$`)
- **unrecognized command** → the input is **echoed back verbatim** (e.g. send
  `huteno$`, get `huteno$`)

## Grammar

`LETTER[,ID[,VALUE...]]$`

- **lowercase letter** = query — e.g. `s,0136C4$` asks for speed. A query can
  include a selector, such as the slot in `p,0136C4,1$`.
- **UPPERCASE letter + value** = set — e.g. `S,0136C4,3$` sets speed 3.
- The device **reports/acknowledges** in uppercase short form **without the
  ID** — e.g. `S,02$`, `M,1$`, or `OK$`.

### The device ID

Every set/query (except `i$` itself) embeds the device's own ID. You don't need
to know it ahead of time — ask for it:

```
-> i$
<- I,0136C4$        # this unit's ID is 0136C4
```

The device ID is a required command prefix, not a secret key. BLE pairing/
encryption is also required on the observed hardware; the ID is not a substitute
for a bond. Android queries `i$` during new-device setup and stores the reply.
HA supplies an ID derived from the final three MAC bytes, based on the existing
hardware observation, not independently confirmed by this APK. Standalone
`DohmClient(device)` defaults to `device_id=None`: connect enables notifications,
queries `i$`, and caches the actual ID. An explicitly supplied ID avoids that
opening query. `get_device_id()` always queries and retains the reported ID.

## Command table

| Purpose | Send | Reply |
|---|---|---|
| Get device ID | `i$` | `I,0136C4$` |
| Query power | `m,0136C4$` | `M,1$` / `M,0$` |
| Set power on/off | `M,0136C4,1$` / `M,0136C4,0$` | `OK$` |
| Query speed | `s,0136C4$` | `S,02$` (zero-padded 2-digit) |
| Set speed | `S,0136C4,3$` (single digit) | `OK$` |
| Query name | `n,0136C4$` | `N,Marpac$` |
| Set name | `N,0136C4,Marpac$` | `OK$` |
| Set time | `T,0136C4,2133,2$` | `OK$` |
| Query schedule slot N | `p,0136C4,1$` | `P,1,2030,0730,7F,02$` |
| Set schedule start | `P,0136C4,1,1,2030$` | `OK$` in capture |
| Set schedule stop | `P,0136C4,2,1,0730$` | `OK$` in capture |
| Set schedule days/volume | `P,0136C4,3,1,7F,02$` | `OK$` in capture |

## Power (`M`/`m`) — IN SCOPE

- `M,<id>,1$` = on, `M,<id>,0$` = off, reply `OK$`.
- `m,<id>$` returns `M,1$` or `M,0$`.

## Speed (`S`/`s`) — IN SCOPE

- `S,<id>,N$` sets speed; reply `OK$`. Value is the bare integer, 1–2 digits
  (`S,<id>,3$`, `S,<id>,10$`).
- `s,<id>$` returns `S,0N$` (zero-padded two digits).
- **Observed active levels: 1–10.** `S,<id>,11$` returns `Failed 03$`. HA maps
  these 10 hardware levels to its entity's volume/speed controls. Saved
  evidence does not establish the firmware response to `S,<id>,0$`.
  The library accepts active speeds 1–10; HA volume/percentage 0 instead sends
  the known power-off command `M,<id>,0$` and preserves the last speed.

## State reads

The characteristic is not GATT-readable, so state is obtained by *querying*:
the original iOS capture polls `m,<id>$` and `s,<id>$` roughly every 5 seconds;
Android schedules both queries every second. HA currently polls every 30 seconds.
State comes from the resulting `M,_$` / `S,0_$` notifications, not a GATT read.

## Time (`T`) — standalone operation, not exposed by HA

`T,<id>,HHMM,D$` where `HHMM` is 24h local time (capture showed `2133` = 21:33)
and `D` is **Monday=1 through Sunday=7**, confirmed by Android's
`LocalDate.getDayOfWeek().getValue()`. No date/timezone/seconds are sent.
The app resyncs on pairing/reconnect. The standalone `set_time(datetime.time,
weekday)` sends this command only when called; it discards seconds. Neither
standalone connect nor HA automatically syncs the clock. Clock changes can
change active onboard schedules. A `t` constant exists in the APK but has no
outbound caller or reply parser; the experimental query below is unconfirmed.

## Schedule (`P`/`p`) — standalone operations, not exposed by HA

Query `p,<id>,N$` returns a consolidated slot:

```
P,<slot>,<startHHMM>,<stopHHMM>,<daysBitmask>,<speed>$
e.g. P,1,2030,0730,7F,02$  ->  slot 1, 20:30–07:30, days 0x7F (all 7), speed 2
```

Android confirms three slots (1–3) and three writes per update, in order:

```
P,<id>,1,<slot>,<startHHMM>$
P,<id>,2,<slot>,<stopHHMM>$
P,<id>,3,<slot>,<daysHex>,<speed02d>$
```

The field selector comes before the slot. Day bits are Monday=`01`,
Tuesday=`02`, Wednesday=`04`, Thursday=`08`, Friday=`10`, Saturday=`20`,
Sunday=`40`; weekdays=`1F`, all days=`7F`. Both day mask and speed are
zero-padded to two characters, mask in uppercase hex and speed in decimal.

In the app model, days=`00` disables a slot and speed=`00` deletes it.
Active speeds are 1–10. The default is 22:00–06:00, weekdays, speed 5.
Deletion still sends all three writes. These disable/delete semantics need
hardware verification. Overnight scheduling is supported by the app UI, but
firmware overlap, equal-time, and overnight-day behavior remain untested.
See [Android findings](android-app.md) for app quirks and validation steps.

The standalone client has `get_schedule(slot)`, `get_schedules()` for all three
slots, and `set_schedule(slot, start, stop, days, speed)`. The setter validates
all fields before sending, holds one operation lock for the three writes and
slot-matched readback, and returns a `ScheduleReport` only after all fields
match at minute precision. It stops at the first failure. Partial updates are
possible after failure or cancellation; there is no rollback. Serialization
is a client guarantee, not a firmware transaction.

## Experimental commands — inferred, not captured or proven

`COMMAND_GET_TIME = "t"` and `COMMAND_SET_UNIT_NUMBER = "I"` in the APK
establish letters only. They do not establish outbound commands or parameters.
`I` is also the known ID report letter. The standalone library exposes these
inferred helpers without asserting firmware support:

| Method | Inferred send | Expected handling (also unproven) |
|---|---|---|
| `get_time()` | `t,<id>$` | Raw `T,...$` bytes; uppercase response letter is inferred, fields unknown |
| `set_device_id(new_device_id)` | `I,<current>,<new>$` | `OK$` (`Ack`), then actual ID query `i$` |

The clock helper does not decode or invent a time schema. Either command can
be echoed, rejected, or time out. Accepted ID values and persistence are
unknown. The ID setter invalidates the cached ID before attempting the write,
and leaves it unknown until readback succeeds. A readback mismatch retains the
actual reported ID and raises `DohmError`; a match returns that ID. There is no
rollback guarantee. Changing an ID can break HA's MAC-derived addressing.
HA never calls these helpers.

## Reply correlation limits

The client matches typed reports, and schedule reads also match the slot.
Unrelated valid reports are ignored. There are **no request IDs** in this
protocol. Same-type stale reports and generic acknowledgements (`OK$`) cannot
be correlated reliably with a current request after retries or cancellation.
Queue draining, serialization, and verified readback reduce risk but cannot
prove which request caused a reply. A timeout or cancellation does not prove
that a write failed; read back state before retrying a deliberate write.

## Notes for the library

- For an unknown device: pair, enable notifications, query `i$`, then use the
  returned ID. Standalone connect does this when no ID is supplied; HA supplies
  its hardware-observed MAC-derived ID instead.
- Name and clock commands are housekeeping, not a recovered secret handshake.
  Hardware behavior still depends on bonding and notification setup.
- Android uses zero-padded ordinary speed writes (`S,<id>,03$`); the existing
  integration's bare decimal (`S,<id>,3$`) is already captured as working.
- The pure protocol module and standalone client include name, clock, and
  schedule operations, plus explicitly experimental clock-query/ID-write
  helpers. See [the full standalone API](library.md) for usage and errors.
- The selected HA entity still exposes only power and speed/volume. Setup
  chooses a media player or ten-step fan; it does not change BLE commands.
  HA does not sync device names, the clock, or schedules, or change device IDs.

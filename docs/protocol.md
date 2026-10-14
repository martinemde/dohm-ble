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
- Also present: Device Information Service `180a` (model/serial/firmware — good
  for the HA device registry) and Battery Service `180f` with battery level
  `2a19` `[read,notify]` (a free HA battery sensor later).
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
The current HA client instead derives it from the final three MAC bytes, based
on the existing hardware observation, not independently confirmed by this APK.

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
- **Confirmed range: 1–10.** `S,<id>,11$` returns `Failed 03$`. HA maps these
  10 hardware levels to its entity's volume/speed controls.

## State reads

The characteristic is not GATT-readable, so state is obtained by *querying*:
the original iOS capture polls `m,<id>$` and `s,<id>$` roughly every 5 seconds;
Android schedules both queries every second. HA currently polls every 30 seconds.
State comes from the resulting `M,_$` / `S,0_$` notifications, not a GATT read.

## Time (`T`) — protocol helper, not exposed by HA

`T,<id>,HHMM,D$` where `HHMM` is 24h local time (capture showed `2133` = 21:33)
and `D` is **Monday=1 through Sunday=7**, confirmed by Android's
`LocalDate.getDayOfWeek().getValue()`. No date/timezone/seconds are sent.
The app resyncs on pairing/reconnect. Only relevant to onboard scheduling,
which the HA integration does not use. A `t` constant exists in the APK but
has no outbound caller or reply parser, so clock queries remain unconfirmed.

## Schedule (`P`/`p`) — protocol helpers, not exposed by HA

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

## Notes for the library

- For an unknown device: pair, enable notifications, query `i$`, then use the
  returned ID. The HA client currently uses MAC-derived IDs instead.
- Name and clock commands are housekeeping, not a recovered secret handshake.
  Hardware behavior still depends on bonding and notification setup.
- Android uses zero-padded ordinary speed writes (`S,<id>,03$`); the existing
  integration's bare decimal (`S,<id>,3$`) is already captured as working.
- The pure protocol module includes name, clock, and schedule helpers. The
  client and HA media player still expose only power and volume.

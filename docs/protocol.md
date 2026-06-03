# Marpac Dohm BLE protocol

Reverse-engineered from a PacketLogger capture of the official Dohm macOS app
(`captures/capture-2133.pklg`, 2026-06-02).

## Transport

- Single characteristic `00005600-d102-11e1-9b23-00025b005aa5` (ATT handle
  `0x001a` in this capture). Writable **with response**, **notify**, **not
  readable**. Its CCCD is handle `0x001b` (write `0100` to subscribe).
- All payloads are UTF-8 strings terminated by `$`.

## Grammar

`LETTER[,ID[,VALUE...]]$`

- **lowercase letter** = query (no value) — e.g. `s,0136C4$` asks for speed.
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

This is the whole "unlock" the official app does that bare LightBlue writes
were missing: there is no secret key — commands just require the ID prefix.

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
| Set schedule (partial) | `P,0136C4,1,1,2030$` | `OK$` |

## Power (`M`/`m`) — IN SCOPE

- `M,<id>,1$` = on, `M,<id>,0$` = off, reply `OK$`.
- `m,<id>$` returns `M,1$` or `M,0$`.

## Speed (`S`/`s`) — IN SCOPE

- `S,<id>,N$` sets speed; reply `OK$`. Set value is a single digit.
- `s,<id>$` returns `S,0N$` (zero-padded two digits).
- Speeds observed in this capture: **2, 3, 4, 5**. Full range not yet confirmed
  (probe `S,<id>,1$`, `S,<id>,0$`, and the upper bound to nail `speed_count`).

## State reads

The characteristic is not GATT-readable, so state is obtained by *querying*:
the app polls `m,<id>$` and `s,<id>$` roughly every 5 seconds and reads the
resulting `M,_$` / `S,0_$` notifications. Our client/coordinator should do the
same to track entity state and detect physical changes.

## Time (`T`) — out of v1 scope

`T,<id>,HHMM,D$` where `HHMM` is 24h local time (capture showed `2133` = 21:33)
and `D` is a weekday index (`2`). The onboard clock drifts, which is why the app
resyncs. Only relevant to the onboard schedule, which v1 does not use.

## Schedule (`P`/`p`) — out of v1 scope (bonus capture)

Query `p,<id>,N$` returns a consolidated slot:

```
P,<slot>,<startHHMM>,<stopHHMM>,<daysBitmask>,<speed>$
e.g. P,1,2030,0730,7F,02$  ->  slot 1, 20:30–07:30, days 0x7F (all 7), speed 2
```

`daysBitmask` `0x7F` = all seven days (one bit per weekday). Setting a slot
appears to be split across multiple writes (`P,<id>,1,1,2030$`,
`P,<id>,2,1,0730$`, `P,<id>,3,1,7F,02$`) — exact field layout NOT yet confirmed;
needs a focused capture if onboard-schedule support is ever pursued.

## Notes for the library

- On connect: enable notifications, send `i$`, capture the returned ID, then use
  it for all subsequent commands. Works on any unit without hardcoding.
- `i$` is almost certainly the only prerequisite to control; the app's `n`/`N`/
  `T` traffic looks like housekeeping. **To verify:** connect fresh, send only
  `i$` then `S,<id>,3$`, and confirm the speed changes without the name/time
  dance. (Validate with `tools/probe.py`.)

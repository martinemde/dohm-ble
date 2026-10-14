"""Pure encode/decode for the Marpac Dohm BLE protocol.

No Bluetooth here — just strings in, strings out. See docs/protocol.md for the
captured grammar. Commands are UTF-8, terminated by ``$``:

- queries are lowercase:                   ``s,<id>$`` or ``p,<id>,<slot>$``
- sets are uppercase with values:           ``S,<id>,<n>$``
- the device reports/acks in uppercase short form without the id: ``S,02$``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

from .const import MAX_SPEED, MIN_SPEED, TERMINATOR

# --- Decoded message types ---------------------------------------------------


@dataclass(frozen=True)
class DeviceId:
    value: str


@dataclass(frozen=True)
class SpeedReport:
    speed: int


@dataclass(frozen=True)
class PowerReport:
    on: bool


@dataclass(frozen=True)
class NameReport:
    name: str


@dataclass(frozen=True)
class ScheduleReport:
    """One schedule slot; days=0 disables it and speed=0 deletes it."""

    slot: int
    start: time
    stop: time
    days: int
    speed: int


@dataclass(frozen=True)
class Ack:
    pass


@dataclass(frozen=True)
class Failure:
    """A rejected command, e.g. ``Failed 03$`` for an out-of-range value."""

    code: str


# --- Encoding ----------------------------------------------------------------


def device_id_from_address(address: str) -> str:
    """The id a device will answer to, from its BLE address.

    The observed id is the lower three MAC bytes in uppercase hex, no
    separators: ``00:22:A3:01:36:C4`` -> ``0136C4`` (docs/debugging.md).
    HA uses this relation to skip an opening query. It is not suitable for
    macOS host UUIDs, and an experimental ID write could invalidate it.
    Standalone callers normally query the actual ID instead.
    """
    return address.replace(":", "").replace("-", "")[-6:].upper()


def query_id() -> bytes:
    """Ask the device for its id. The only command that needs no id.

    Standalone connect uses this when no ID is supplied. HA skips it by
    supplying the observed MAC-derived ID. It can also check that derivation
    or recover the actual ID after an experimental ID-write attempt.
    """
    return f"i{TERMINATOR}".encode()


def query_power(device_id: str) -> bytes:
    _validate_field(device_id, "device_id")
    return f"m,{device_id}{TERMINATOR}".encode()


def query_speed(device_id: str) -> bytes:
    _validate_field(device_id, "device_id")
    return f"s,{device_id}{TERMINATOR}".encode()


def set_power(device_id: str, on: bool) -> bytes:
    _validate_field(device_id, "device_id")
    if type(on) is not bool:
        raise TypeError("on must be a bool")
    return f"M,{device_id},{1 if on else 0}{TERMINATOR}".encode()


def set_speed(device_id: str, speed: int) -> bytes:
    _validate_field(device_id, "device_id")
    _validate_int(speed, "speed", MIN_SPEED, MAX_SPEED)
    return f"S,{device_id},{speed}{TERMINATOR}".encode()


def _validate_field(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must be a nonempty string")
    if any(char in ",$" or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains a delimiter or control character")


def _validate_int(value: int, field: str, minimum: int, maximum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")


def _format_time(value: time, field: str) -> str:
    if not isinstance(value, time):
        raise TypeError(f"{field} must be a datetime.time")
    # Android's LocalTime formatter uses HHmm, discarding seconds/subseconds.
    return f"{value.hour:02d}{value.minute:02d}"


def query_name(device_id: str) -> bytes:
    _validate_field(device_id, "device_id")
    return f"n,{device_id}{TERMINATOR}".encode()


def set_name(device_id: str, name: str) -> bytes:
    """Set a UTF-8 name, rejecting command delimiters and control characters.

    The Android UI limits names to ten alphanumeric characters. A hardware
    length limit is not established, so this encoder does not impose one.
    """
    _validate_field(device_id, "device_id")
    _validate_field(name, "name")
    return f"N,{device_id},{name}{TERMINATOR}".encode()


def set_time(device_id: str, value: time, weekday: int) -> bytes:
    """Set local HHMM and ISO weekday (Monday=1, Sunday=7)."""
    _validate_field(device_id, "device_id")
    _validate_int(weekday, "weekday", 1, 7)
    hhmm = _format_time(value, "time")
    return f"T,{device_id},{hhmm},{weekday}{TERMINATOR}".encode()


def query_time(device_id: str) -> bytes:
    """Experimental: the APK declares t, but never sends it.

    This follows the query grammar; firmware support and reply fields are unknown.
    """
    _validate_field(device_id, "device_id")
    return f"t,{device_id}{TERMINATOR}".encode()


def set_device_id(device_id: str, new_device_id: str) -> bytes:
    """Experimental: inferred I,<current ID>,<new ID>$, not a captured write.

    The APK declares I but uses it only for ID replies. This may be rejected
    or echoed, and accepted values/persistence are unknown.
    """
    _validate_field(device_id, "device_id")
    _validate_field(new_device_id, "new_device_id")
    return f"I,{device_id},{new_device_id}{TERMINATOR}".encode()


def query_schedule(device_id: str, slot: int) -> bytes:
    _validate_field(device_id, "device_id")
    _validate_int(slot, "slot", 1, 3)
    return f"p,{device_id},{slot}{TERMINATOR}".encode()


def set_schedule(
    device_id: str,
    slot: int,
    start: time,
    stop: time,
    days: int,
    speed: int,
) -> tuple[bytes, bytes, bytes]:
    """Encode Android's ordered start, stop, and days/speed writes.

    The seven-bit day mask is hex; speed is two-digit decimal. A zero mask
    disables a schedule. Zero speed deletes a slot, not a live speed setting.
    """
    _validate_field(device_id, "device_id")
    _validate_int(slot, "slot", 1, 3)
    _validate_int(days, "days", 0, 127)
    _validate_int(speed, "speed", 0, 10)
    start_hhmm = _format_time(start, "start")
    stop_hhmm = _format_time(stop, "stop")
    return (
        f"P,{device_id},1,{slot},{start_hhmm}{TERMINATOR}".encode(),
        f"P,{device_id},2,{slot},{stop_hhmm}{TERMINATOR}".encode(),
        f"P,{device_id},3,{slot},{days:02X},{speed:02d}{TERMINATOR}".encode(),
    )


# --- Decoding ----------------------------------------------------------------


def _parse_time(value: str, field: str) -> time:
    if re.fullmatch(r"[0-9]{4}", value) is None:
        raise ValueError(f"{field} must contain exactly four HHMM digits")
    try:
        return time(int(value[:2]), int(value[2:]))
    except ValueError as err:
        raise ValueError(f"{field} is not a valid HHMM time: {value!r}") from err


def _parse_schedule(rest: str) -> ScheduleReport:
    fields = rest.split(",")
    if len(fields) != 5:
        raise ValueError("schedule report must contain slot,start,stop,days,speed")
    slot_text, start_text, stop_text, days_text, speed_text = fields
    if re.fullmatch(r"[0-9]+", slot_text) is None:
        raise ValueError("schedule slot must be decimal digits")
    if re.fullmatch(r"[0-9A-Fa-f]+", days_text) is None:
        raise ValueError("schedule days must be hexadecimal digits")
    if re.fullmatch(r"[0-9]+", speed_text) is None:
        raise ValueError("schedule speed must be decimal digits")
    slot, days, speed = int(slot_text), int(days_text, 16), int(speed_text)
    _validate_int(slot, "slot", 1, 3)
    _validate_int(days, "days", 0, 127)
    _validate_int(speed, "speed", 0, 10)
    return ScheduleReport(
        slot,
        _parse_time(start_text, "start"),
        _parse_time(stop_text, "stop"),
        days,
        speed,
    )


def parse(
    payload: bytes,
) -> DeviceId | SpeedReport | PowerReport | NameReport | ScheduleReport | Ack | Failure:
    """Decode a device reply/notification into a typed message."""
    text = payload.decode()
    if not text.endswith(TERMINATOR):
        raise ValueError(f"missing terminator: {payload!r}")
    body = text[: -len(TERMINATOR)]

    if body == "OK":
        return Ack()
    if body.startswith("Failed"):
        return Failure(body.removeprefix("Failed").strip())

    letter, _, rest = body.partition(",")
    if letter == "I":
        _validate_field(rest, "device_id")
        return DeviceId(rest)
    if letter == "S":
        if re.fullmatch(r"[0-9]+", rest) is None:
            raise ValueError("speed must be decimal digits")
        speed = int(rest)
        _validate_int(speed, "speed", MIN_SPEED, MAX_SPEED)
        return SpeedReport(speed)
    if letter == "M":
        if rest not in ("0", "1"):
            raise ValueError("power must be 0 or 1")
        return PowerReport(rest == "1")
    if letter == "N":
        _validate_field(rest, "name")
        return NameReport(rest)
    if letter == "P":
        return _parse_schedule(rest)

    raise ValueError(f"unknown message: {payload!r}")

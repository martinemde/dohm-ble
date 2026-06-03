"""Pure encode/decode for the Marpac Dohm BLE protocol.

No Bluetooth here — just strings in, strings out. See docs/protocol.md for the
captured grammar. Commands are UTF-8, terminated by ``$``:

- queries are lowercase with no value:      ``s,<id>$``
- sets are uppercase with a value:          ``S,<id>,<n>$``
- the device reports/acks in uppercase short form without the id: ``S,02$``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import TERMINATOR


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
class Ack:
    pass


# --- Encoding ----------------------------------------------------------------

def query_id() -> bytes:
    """Ask the device for its id. The only command that needs no id."""
    return f"i{TERMINATOR}".encode()


def query_power(device_id: str) -> bytes:
    return f"m,{device_id}{TERMINATOR}".encode()


def query_speed(device_id: str) -> bytes:
    return f"s,{device_id}{TERMINATOR}".encode()


def set_power(device_id: str, on: bool) -> bytes:
    return f"M,{device_id},{1 if on else 0}{TERMINATOR}".encode()


def set_speed(device_id: str, speed: int) -> bytes:
    return f"S,{device_id},{speed}{TERMINATOR}".encode()


# --- Decoding ----------------------------------------------------------------

def parse(payload: bytes) -> DeviceId | SpeedReport | PowerReport | NameReport | Ack:
    """Decode a device reply/notification into a typed message."""
    text = payload.decode()
    if not text.endswith(TERMINATOR):
        raise ValueError(f"missing terminator: {payload!r}")
    body = text[: -len(TERMINATOR)]

    if body == "OK":
        return Ack()

    letter, _, rest = body.partition(",")
    if letter == "I":
        return DeviceId(rest)
    if letter == "S":
        return SpeedReport(int(rest))
    if letter == "M":
        return PowerReport(rest == "1")
    if letter == "N":
        return NameReport(rest)

    raise ValueError(f"unknown message: {payload!r}")

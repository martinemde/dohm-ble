"""Tests for the pure Dohm protocol encode/decode layer.

Every expected byte string here is taken verbatim from the PacketLogger
capture documented in docs/protocol.md (device id 0136C4).
"""

import pytest

from custom_components.dohm import protocol

ID = "0136C4"


# --- Encoding: queries (lowercase, no value) ---------------------------------

def test_query_id_needs_no_device_id():
    assert protocol.query_id() == b"i$"


def test_query_power():
    assert protocol.query_power(ID) == b"m,0136C4$"


def test_query_speed():
    assert protocol.query_speed(ID) == b"s,0136C4$"


# --- Encoding: sets (uppercase, with value) ----------------------------------

def test_set_speed_uses_single_digit():
    assert protocol.set_speed(ID, 3) == b"S,0136C4,3$"


def test_set_power_on():
    assert protocol.set_power(ID, True) == b"M,0136C4,1$"


def test_set_power_off():
    assert protocol.set_power(ID, False) == b"M,0136C4,0$"


def test_set_speed_two_digit_max_is_not_padded():
    # Speed 10 is the confirmed maximum; sets send the bare integer.
    assert protocol.set_speed(ID, 10) == b"S,0136C4,10$"


# --- Decoding: device replies and notifications ------------------------------

def test_parse_device_id_reply():
    assert protocol.parse(b"I,0136C4$") == protocol.DeviceId("0136C4")


def test_parse_speed_report_strips_zero_padding():
    assert protocol.parse(b"S,02$") == protocol.SpeedReport(2)


def test_parse_speed_report_two_digit():
    assert protocol.parse(b"S,10$") == protocol.SpeedReport(10)


def test_parse_failure_reply():
    # The device rejects bad values with "Failed <code>$" (space, not comma).
    assert protocol.parse(b"Failed 03$") == protocol.Failure("03")


def test_parse_power_on_report():
    assert protocol.parse(b"M,1$") == protocol.PowerReport(True)


def test_parse_power_off_report():
    assert protocol.parse(b"M,0$") == protocol.PowerReport(False)


def test_parse_ack():
    assert protocol.parse(b"OK$") == protocol.Ack()


def test_parse_name_report():
    assert protocol.parse(b"N,Marpac$") == protocol.NameReport("Marpac")


# --- Decoding: error handling ------------------------------------------------

def test_parse_rejects_payload_without_terminator():
    with pytest.raises(ValueError):
        protocol.parse(b"S,02")


def test_parse_unknown_message_raises():
    with pytest.raises(ValueError):
        protocol.parse(b"Z,99$")

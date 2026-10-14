"""Tests for the pure Dohm protocol encode/decode layer.

Power/speed bytes come from PacketLogger (device id 0136C4). Name, time,
and schedule bytes also use the decompiled Android app's CommandBuilder,
Device command methods, and BluetoothService notification parser.
"""

from datetime import time

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


def test_device_id_is_the_lower_three_address_bytes():
    # docs/debugging.md: the Dohm at 00:22:A3:01:36:C4 answers to 0136C4, and
    # commands that carry any other id are rejected. Deriving it is what lets
    # the connect path skip i$ entirely.
    assert protocol.device_id_from_address("00:22:A3:01:36:C4") == "0136C4"


def test_device_id_normalises_separators_and_case():
    # Home Assistant hands addresses over in upper-case colon form, but ESPHome
    # proxies and bleak backends have both been seen using dashes or lower case.
    for address in ("00:22:a3:01:36:c4", "00-22-A3-01-36-C4", "0022A30136C4"):
        assert protocol.device_id_from_address(address) == "0136C4"


# --- Android-derived name, time, and schedule commands -----------------------


def test_query_name():
    assert protocol.query_name(ID) == b"n,0136C4$"


def test_set_name():
    assert protocol.set_name(ID, "Marpac") == b"N,0136C4,Marpac$"


def test_set_name_does_not_invent_hardware_length_or_ascii_limits():
    # The UI has a ten-character limit, not a verified protocol/hardware limit.
    name = "BedroomDohmÉ"
    assert protocol.set_name(ID, name) == f"N,{ID},{name}$".encode()


@pytest.mark.parametrize("weekday", [1, 7])
def test_set_time_uses_hhmm_and_iso_weekday(weekday):
    assert protocol.set_time(ID, time(6, 5, 59, 123), weekday) == (
        f"T,{ID},0605,{weekday}$".encode()
    )


def test_set_time_end_of_day():
    assert protocol.set_time(ID, time(23, 59), 3) == b"T,0136C4,2359,3$"


@pytest.mark.parametrize("slot", [1, 2, 3])
def test_query_schedule(slot):
    assert protocol.query_schedule(ID, slot) == f"p,{ID},{slot}$".encode()


def test_set_schedule_matches_android_three_write_order_and_padding():
    assert protocol.set_schedule(ID, 2, time(22, 5), time(6, 30), 0x3E, 3) == (
        b"P,0136C4,1,2,2205$",
        b"P,0136C4,2,2,0630$",
        b"P,0136C4,3,2,3E,03$",
    )


def test_set_schedule_all_days_and_max_speed():
    assert protocol.set_schedule(ID, 3, time(0), time(23, 59), 127, 10) == (
        b"P,0136C4,1,3,0000$",
        b"P,0136C4,2,3,2359$",
        b"P,0136C4,3,3,7F,10$",
    )


def test_set_schedule_disabled_mask_and_delete_speed():
    assert protocol.set_schedule(ID, 1, time(0), time(0), 0, 0) == (
        b"P,0136C4,1,1,0000$",
        b"P,0136C4,2,1,0000$",
        b"P,0136C4,3,1,00,00$",
    )


def test_set_schedule_single_day_and_seconds_discarded():
    assert protocol.set_schedule(ID, 1, time(8, 9, 10), time(8, 9, 59), 1, 1) == (
        b"P,0136C4,1,1,0809$",
        b"P,0136C4,2,1,0809$",
        b"P,0136C4,3,1,01,01$",
    )


NEW_ENCODERS = [
    protocol.query_name,
    lambda device_id: protocol.set_name(device_id, "Marpac"),
    lambda device_id: protocol.set_time(device_id, time(0), 1),
    lambda device_id: protocol.query_schedule(device_id, 1),
    lambda device_id: protocol.set_schedule(device_id, 1, time(0), time(0), 0, 0),
]


@pytest.mark.parametrize("encoder", NEW_ENCODERS)
@pytest.mark.parametrize("device_id", ["", None, "0136,C4", "0136C4$", "ID\n", "ID\0"])
def test_new_encoders_reject_invalid_id_fields(encoder, device_id):
    with pytest.raises((TypeError, ValueError), match="device_id"):
        encoder(device_id)


@pytest.mark.parametrize("name", ["", None, "A,B", "A$", "A\r", "A\n", "A\0", "A\x7f"])
def test_set_name_rejects_empty_names_and_delimiter_or_control_injection(name):
    with pytest.raises((TypeError, ValueError), match="name"):
        protocol.set_name(ID, name)


@pytest.mark.parametrize("weekday", [0, 8, -1, 1.0, True, "1,$"])
def test_set_time_rejects_invalid_weekdays(weekday):
    with pytest.raises((TypeError, ValueError), match="weekday"):
        protocol.set_time(ID, time(0), weekday)


@pytest.mark.parametrize("value", [None, "1200", "1200$", 1200])
def test_set_time_rejects_non_time_values(value):
    with pytest.raises(TypeError, match="datetime.time"):
        protocol.set_time(ID, value, 1)


@pytest.mark.parametrize("slot", [0, 4, -1, 1.0, True, "1,$"])
def test_schedule_encoders_reject_invalid_slots(slot):
    with pytest.raises((TypeError, ValueError), match="slot"):
        protocol.query_schedule(ID, slot)
    with pytest.raises((TypeError, ValueError), match="slot"):
        protocol.set_schedule(ID, slot, time(0), time(0), 1, 1)


@pytest.mark.parametrize("days", [-1, 128, 255, 1.0, True, "7F", "7F,$"])
def test_set_schedule_rejects_invalid_day_masks(days):
    with pytest.raises((TypeError, ValueError), match="days"):
        protocol.set_schedule(ID, 1, time(0), time(0), days, 1)


@pytest.mark.parametrize("speed", [-1, 11, 1.0, True, "01", "01,$"])
def test_set_schedule_rejects_invalid_speeds(speed):
    with pytest.raises((TypeError, ValueError), match="speed"):
        protocol.set_schedule(ID, 1, time(0), time(0), 1, speed)


@pytest.mark.parametrize("value", [None, "1200", "1200,$", 1200])
@pytest.mark.parametrize("field", ["start", "stop"])
def test_set_schedule_rejects_non_time_values(value, field):
    values = {"start": time(0), "stop": time(0)}
    values[field] = value
    with pytest.raises(TypeError, match=field):
        protocol.set_schedule(ID, 1, values["start"], values["stop"], 1, 1)


# --- Android-derived schedule notifications ---------------------------------


def test_parse_schedule_report():
    assert protocol.parse(b"P,2,2205,0630,3E,03$") == protocol.ScheduleReport(
        slot=2,
        start=time(22, 5),
        stop=time(6, 30),
        days=0x3E,
        speed=3,
    )


def test_parse_schedule_report_all_days_and_max_speed():
    assert protocol.parse(b"P,3,0000,2359,7F,10$") == protocol.ScheduleReport(
        slot=3,
        start=time(0),
        stop=time(23, 59),
        days=127,
        speed=10,
    )


def test_parse_schedule_report_disabled_and_deleted():
    assert protocol.parse(b"P,1,0000,0000,00,00$") == protocol.ScheduleReport(
        slot=1,
        start=time(0),
        stop=time(0),
        days=0,
        speed=0,
    )


def test_parse_schedule_report_accepts_lowercase_hex_and_unpadded_speed():
    assert protocol.parse(b"P,1,0100,0200,0a,2$") == protocol.ScheduleReport(
        slot=1,
        start=time(1),
        stop=time(2),
        days=10,
        speed=2,
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"P,1,0000,0000,00,00", "terminator"),
        (b"P,1,0000,0000,00$", "must contain"),
        (b"P,1,0000,0000,00,00,extra$", "must contain"),
        (b"P,0136C4,1,0000,0000,00,00$", "must contain"),
        (b"P,0,0000,0000,00,00$", "slot"),
        (b"P,4,0000,0000,00,00$", "slot"),
        (b"P,-1,0000,0000,00,00$", "slot"),
        (b"P,x,0000,0000,00,00$", "slot"),
        (b"P,1,2400,0000,00,00$", "start"),
        (b"P,1,1260,0000,00,00$", "start"),
        (b"P,1,000,0000,00,00$", "start"),
        (b"P,1,00000,0000,00,00$", "start"),
        (b"P,1,00x0,0000,00,00$", "start"),
        (b"P,1,0000,2400,00,00$", "stop"),
        (b"P,1,0000,1260,00,00$", "stop"),
        (b"P,1,0000,,00,00$", "stop"),
        (b"P,1,0000,0000,80,00$", "days"),
        (b"P,1,0000,0000,FF,00$", "days"),
        (b"P,1,0000,0000,-1,00$", "days"),
        (b"P,1,0000,0000,0x7F,00$", "days"),
        (b"P,1,0000,0000,GG,00$", "days"),
        (b"P,1,0000,0000,,00$", "days"),
        (b"P,1,0000,0000,00,11$", "speed"),
        (b"P,1,0000,0000,00,-1$", "speed"),
        (b"P,1,0000,0000,00,0A$", "speed"),
        (b"P,1,0000,0000,00,+1$", "speed"),
        (b"P,1,0000,0000,00, 1$", "speed"),
        (b"P,1,0000,0000,00,$", "speed"),
        (b"P,1,0000,0000,00,00$$", "speed"),
        ("P,1,００００,0000,00,00$".encode(), "start"),
    ],
)
def test_parse_rejects_invalid_schedule_reports(payload, error):
    with pytest.raises(ValueError, match=error):
        protocol.parse(payload)

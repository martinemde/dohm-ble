"""Standalone command API tests; no Home Assistant or live BLE is required."""

import asyncio
from datetime import time

import pytest
from test_client import DEVICE_ID, FakeDohm

from custom_components.dohm import client as client_module
from custom_components.dohm import protocol
from custom_components.dohm.client import DohmClient, DohmCommandError, DohmError


class CommandDohm(FakeDohm):
    """Extend the captured device fake with names, clock, and schedule slots."""

    def __init__(self):
        super().__init__()
        self.name = "Bedroom"
        self.clock = None
        self.schedules = {
            slot: protocol.ScheduleReport(
                slot, time(22, slot), time(7, slot), 0x3E, slot
            )
            for slot in range(1, 4)
        }
        self.replies = {}
        self.delayed_replies = {}
        self.timers = []

    def _respond(self, text):
        if text in self.replies:
            return self.replies[text]
        fields = text[:-1].split(",")
        letter = fields[0]
        if letter == "n":
            return f"N,{self.name}$"
        if letter == "N":
            self.name = fields[2]
            return "OK$"
        if letter == "T":
            self.clock = (fields[2], int(fields[3]))
            return "OK$"
        if letter == "p":
            report = self.schedules[int(fields[2])]
            return (
                f"P,{report.slot},{report.start:%H%M},{report.stop:%H%M},"
                f"{report.days:02X},{report.speed:02d}$"
            )
        if letter == "P":
            part, slot = int(fields[2]), int(fields[3])
            report = self.schedules[slot]
            start, stop, days, speed = (
                report.start,
                report.stop,
                report.days,
                report.speed,
            )
            if part == 1:
                start = time(int(fields[4][:2]), int(fields[4][2:]))
            elif part == 2:
                stop = time(int(fields[4][:2]), int(fields[4][2:]))
            else:
                days, speed = int(fields[4], 16), int(fields[5])
            self.schedules[slot] = protocol.ScheduleReport(
                slot, start, stop, days, speed
            )
            return "OK$"
        return super()._respond(text)

    def emit(self, frame):
        if self._notify is not None:
            self._notify(0, bytearray(frame.encode()))

    async def write_gatt_char(self, _char, data, response=True):
        self.writes.append(bytes(data))
        text = bytes(data).decode()
        reply = self._respond(text)
        if self._notify is None or not self._armed:
            return
        if text in self.delayed_replies:
            loop = asyncio.get_running_loop()
            for delay, frame in self.delayed_replies[text]:
                self.timers.append(loop.call_later(delay, self.emit, frame))
            return
        if reply is None:
            return
        for frame in reply if isinstance(reply, list) else [reply]:
            self.emit(frame)

    async def disconnect(self):
        for timer in self.timers:
            timer.cancel()
        await super().disconnect()


@pytest.fixture
async def command_pair(monkeypatch):
    # Bound unexpected replies too, so a regression cannot stall the suite.
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(client_module, "OPENING_TIMEOUT", 0.2)
    monkeypatch.setattr(client_module, "OPENING_ATTEMPTS", 1)
    fake = CommandDohm()

    async def connector(_device):
        return fake

    client = DohmClient(object(), device_id=DEVICE_ID, connector=connector)
    await client.connect()
    try:
        yield client, fake
    finally:
        await client.disconnect()


async def test_connect_discovers_only_a_missing_id():
    fake = CommandDohm()

    async def connector(_device):
        return fake

    client = DohmClient(object(), connector=connector)
    assert client.device_id is None
    try:
        await client.connect()
        assert client.device_id == DEVICE_ID
        assert fake.writes == [b"i$"]
        await client.connect()
        assert fake.writes == [b"i$"]
        assert await client.get_name() == "Bedroom"
        assert fake.writes[-1] == b"n,0136C4$"
    finally:
        await client.disconnect()


async def test_explicit_id_skips_discovery(command_pair):
    client, fake = command_pair
    assert client.device_id == DEVICE_ID
    assert fake.writes == []
    await client.get_name()
    assert fake.writes == [b"n,0136C4$"]


async def test_get_device_id_queries_and_updates_stored_id(command_pair):
    client, fake = command_pair
    fake.device_id = "ABCDEF"
    assert await client.get_device_id() == "ABCDEF"
    assert client.device_id == "ABCDEF"
    assert await client.set_name("Office") is None
    assert fake.writes == [b"i$", b"N,ABCDEF,Office$"]


async def test_name_commands_use_utf8_and_return_name(command_pair):
    client, fake = command_pair
    assert await client.get_name() == "Bedroom"
    assert await client.set_name("Døhm room") is None
    assert await client.get_name() == "Døhm room"
    assert fake.writes == [
        b"n,0136C4$",
        "N,0136C4,Døhm room$".encode(),
        b"n,0136C4$",
    ]


async def test_set_time_encodes_minute_precision_and_iso_weekday(command_pair):
    client, fake = command_pair
    assert await client.set_time(time(8, 9, 42, 123456), 7) is None
    assert fake.clock == ("0809", 7)
    assert fake.writes == [b"T,0136C4,0809,7$"]


async def test_get_schedule_returns_typed_slot(command_pair):
    client, fake = command_pair
    assert await client.get_schedule(2) == fake.schedules[2]
    assert fake.writes == [b"p,0136C4,2$"]


async def test_get_schedules_returns_all_three_in_order(command_pair):
    client, fake = command_pair
    result = await client.get_schedules()
    assert isinstance(result, tuple)
    assert result == tuple(fake.schedules.values())
    assert fake.writes == [b"p,0136C4,1$", b"p,0136C4,2$", b"p,0136C4,3$"]


@pytest.mark.parametrize("days,speed", [(0x55, 10), (0, 4), (0x7F, 0)])
async def test_set_schedule_writes_in_order_then_reads_back(command_pair, days, speed):
    client, fake = command_pair
    result = await client.set_schedule(
        2, time(21, 8, 59, 999999), time(6, 9, 12), days, speed
    )
    assert result == protocol.ScheduleReport(2, time(21, 8), time(6, 9), days, speed)
    assert fake.writes == [
        b"P,0136C4,1,2,2108$",
        b"P,0136C4,2,2,0609$",
        f"P,0136C4,3,2,{days:02X},{speed:02d}$".encode(),
        b"p,0136C4,2$",
    ]


@pytest.mark.parametrize(
    "method,args,error",
    [
        ("set_name", ("",), ValueError),
        ("set_name", ("a,b",), ValueError),
        ("set_name", ("a$b",), ValueError),
        ("set_name", ("a\nb",), ValueError),
        ("set_name", (None,), TypeError),
        ("set_time", (time(12), 0), ValueError),
        ("set_time", (time(12), 8), ValueError),
        ("set_time", (time(12), True), TypeError),
        ("set_time", ("1200", 1), TypeError),
        ("get_schedule", (0,), ValueError),
        ("get_schedule", (4,), ValueError),
        ("get_schedule", (True,), TypeError),
        ("get_schedule", (1.0,), TypeError),
        ("set_schedule", (0, time(21), time(7), 1, 1), ValueError),
        ("set_schedule", (4, time(21), time(7), 1, 1), ValueError),
        ("set_schedule", (True, time(21), time(7), 1, 1), TypeError),
        ("set_schedule", (1, "2100", time(7), 1, 1), TypeError),
        ("set_schedule", (1, time(21), "0700", 1, 1), TypeError),
        ("set_schedule", (1, time(21), time(7), -1, 1), ValueError),
        ("set_schedule", (1, time(21), time(7), 128, 1), ValueError),
        ("set_schedule", (1, time(21), time(7), True, 1), TypeError),
        ("set_schedule", (1, time(21), time(7), 1, -1), ValueError),
        ("set_schedule", (1, time(21), time(7), 1, 11), ValueError),
        ("set_schedule", (1, time(21), time(7), 1, True), TypeError),
    ],
)
async def test_invalid_input_never_writes(command_pair, method, args, error):
    client, fake = command_pair
    with pytest.raises(error):
        await getattr(client, method)(*args)
    assert fake.writes == []
    assert client.is_connected


@pytest.mark.parametrize(
    "method,args,wire,replies,expected",
    [
        ("get_device_id", (), "i$", ["S,02$", "I,ABCDEF$"], "ABCDEF"),
        ("get_name", (), "n,0136C4$", ["M,1$", "N,Office$"], "Office"),
        ("get_speed", (), "s,0136C4$", ["N,Office$", "S,07$"], 7),
        ("get_power", (), "m,0136C4$", ["S,09$", "M,0$"], False),
        ("set_name", ("Office",), "N,0136C4,Office$", ["N,Office$", "OK$"], None),
        ("set_time", (time(8), 1), "T,0136C4,0800,1$", ["M,1$", "OK$"], None),
        ("set_speed", (4,), "S,0136C4,4$", ["S,04$", "OK$"], None),
        ("set_power", (False,), "M,0136C4,0$", ["M,0$", "OK$"], None),
    ],
)
async def test_commands_ignore_valid_wrong_report_types(
    command_pair, method, args, wire, replies, expected
):
    client, fake = command_pair
    fake.replies[wire] = replies
    assert await getattr(client, method)(*args) == expected
    assert fake.writes == [wire.encode()]


async def test_schedule_query_ignores_wrong_type_and_wrong_slot(command_pair):
    client, fake = command_pair
    fake.replies["p,0136C4,2$"] = [
        "OK$",
        "N,Office$",
        "P,1,2200,0700,7F,01$",
        "P,3,2200,0700,7F,03$",
        "P,2,2100,0600,55,04$",
    ]
    assert await client.get_schedule(2) == protocol.ScheduleReport(
        2, time(21), time(6), 0x55, 4
    )


async def test_ack_is_not_a_query_response(command_pair, monkeypatch):
    client, fake = command_pair
    await client.get_power()  # Use the normal timeout, not the opening grace.
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.01)
    fake.replies["n,0136C4$"] = "OK$"
    with pytest.raises(TimeoutError):
        await client.get_name()
    assert not client.is_connected


async def test_unrelated_reports_do_not_restart_timeout_budget(
    command_pair, monkeypatch
):
    client, fake = command_pair
    await client.get_power()
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.04)
    fake.delayed_replies["n,0136C4$"] = [
        (0.015, "S,02$"),
        (0.03, "M,1$"),
        (0.055, "N,Too late$"),
    ]
    with pytest.raises(TimeoutError):
        await client.get_name()
    assert not client.is_connected


@pytest.mark.parametrize(
    "method,args,wire,bad_reply",
    [
        ("get_name", (), "n,0136C4$", "n,0136C4$"),
        ("set_name", ("Office",), "N,0136C4,Office$", "N,0136C4,Office$"),
        ("set_time", (time(8), 1), "T,0136C4,0800,1$", "T,0136C4,0800,1$"),
        ("get_speed", (), "s,0136C4$", "S,not-a-number$"),
        ("get_name", (), "n,0136C4$", "N,unterminated"),
        ("get_schedule", (1,), "p,0136C4,1$", "P,1,2500,0700,7F,02$"),
    ],
)
async def test_malformed_or_echo_reply_raises_protocol_error(
    command_pair, method, args, wire, bad_reply
):
    client, fake = command_pair
    fake.replies[wire] = bad_reply
    with pytest.raises(client_module.DohmProtocolError) as exc:
        await getattr(client, method)(*args)
    assert isinstance(exc.value, DohmError)


@pytest.mark.parametrize(
    "method,args,wire",
    [
        ("get_name", (), "n,0136C4$"),
        ("get_device_id", (), "i$"),
        ("set_name", ("Office",), "N,0136C4,Office$"),
        ("set_time", (time(8), 1), "T,0136C4,0800,1$"),
        ("get_schedule", (2,), "p,0136C4,2$"),
    ],
)
async def test_rejected_command_keeps_connection(command_pair, method, args, wire):
    client, fake = command_pair
    fake.replies[wire] = "Failed 05$"
    with pytest.raises(DohmCommandError, match="05"):
        await getattr(client, method)(*args)
    assert client.is_connected
    assert fake.is_connected
    assert await client.get_power() is True


@pytest.mark.parametrize("failure_index", [0, 1, 2, 3])
async def test_schedule_rejection_stops_sequence_without_rollback(
    command_pair, failure_index
):
    client, fake = command_pair
    original = fake.schedules[1]
    wires = [
        b"P,0136C4,1,1,2100$",
        b"P,0136C4,2,1,0600$",
        b"P,0136C4,3,1,55,04$",
        b"p,0136C4,1$",
    ]
    fake.replies[wires[failure_index].decode()] = "Failed 03$"
    with pytest.raises(DohmCommandError):
        await client.set_schedule(1, time(21), time(6), 0x55, 4)
    assert fake.writes == wires[: failure_index + 1]
    assert fake.schedules[1] == protocol.ScheduleReport(
        1,
        time(21) if failure_index >= 1 else original.start,
        time(6) if failure_index >= 2 else original.stop,
        0x55 if failure_index >= 3 else original.days,
        4 if failure_index >= 3 else original.speed,
    )
    assert client.is_connected


@pytest.mark.parametrize(
    "readback",
    [
        "P,1,2101,0600,55,04$",
        "P,1,2100,0601,55,04$",
        "P,1,2100,0600,54,04$",
        "P,1,2100,0600,55,05$",
    ],
)
async def test_schedule_readback_mismatch_raises_without_rollback(
    command_pair, readback
):
    client, fake = command_pair
    fake.replies["p,0136C4,1$"] = readback
    with pytest.raises(DohmError):
        await client.set_schedule(1, time(21), time(6), 0x55, 4)
    assert len(fake.writes) == 4
    assert fake.schedules[1] == protocol.ScheduleReport(1, time(21), time(6), 0x55, 4)


async def _block_after_write(fake, wire):
    started, resume = asyncio.Event(), asyncio.Event()
    original = fake.write_gatt_char

    async def blocked(char, data, response=True):
        await original(char, data, response=response)
        if bytes(data) == wire:
            started.set()
            await resume.wait()

    fake.write_gatt_char = blocked
    return started, resume


@pytest.mark.parametrize("operation", ["get_schedules", "set_schedule"])
async def test_schedule_operation_prevents_interleaved_poll_and_write(
    command_pair, operation
):
    client, fake = command_pair
    if operation == "get_schedules":
        expected = [b"p,0136C4,1$", b"p,0136C4,2$", b"p,0136C4,3$"]
        run = client.get_schedules()
    else:
        expected = [
            b"P,0136C4,1,1,2100$",
            b"P,0136C4,2,1,0600$",
            b"P,0136C4,3,1,55,04$",
            b"p,0136C4,1$",
        ]
        run = client.set_schedule(1, time(21), time(6), 0x55, 4)
    started, resume = await _block_after_write(fake, expected[0])
    schedule = asyncio.create_task(run)
    await asyncio.wait_for(started.wait(), 1)
    poll = asyncio.create_task(client.get_power())
    rename = asyncio.create_task(client.set_name("Office"))
    try:
        await asyncio.sleep(0)
        assert fake.writes == expected[:1]
    finally:
        resume.set()
        await asyncio.gather(schedule, poll, rename)
    assert fake.writes == expected + [b"m,0136C4$", b"N,0136C4,Office$"]


async def test_device_id_update_is_locked_with_query(command_pair):
    client, fake = command_pair
    fake.device_id = "ABCDEF"
    started, resume = await _block_after_write(fake, b"i$")
    discovering = asyncio.create_task(client.get_device_id())
    await asyncio.wait_for(started.wait(), 1)
    rename = asyncio.create_task(client.set_name("Office"))
    try:
        await asyncio.sleep(0)
        assert fake.writes == [b"i$"]
    finally:
        resume.set()
        await asyncio.gather(discovering, rename)
    assert client.device_id == "ABCDEF"
    assert fake.writes == [b"i$", b"N,ABCDEF,Office$"]


@pytest.mark.parametrize("operation", ["get_schedules", "set_schedule"])
async def test_cancellation_releases_schedule_lock_without_rollback(
    command_pair, operation
):
    client, fake = command_pair
    original = fake.schedules[1]
    if operation == "get_schedules":
        wire = b"p,0136C4,2$"
        run = client.get_schedules()
        expected = [b"p,0136C4,1$", wire]
    else:
        wire = b"P,0136C4,2,1,0600$"
        run = client.set_schedule(1, time(21), time(6), 0x55, 4)
        expected = [b"P,0136C4,1,1,2100$", wire]
    started, resume = await _block_after_write(fake, wire)
    task = asyncio.create_task(run)
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resume.set()
    assert fake.writes == expected
    assert client.is_connected
    assert await asyncio.wait_for(client.get_power(), 1) is True
    if operation == "set_schedule":
        assert fake.schedules[1] == protocol.ScheduleReport(
            1, time(21), time(6), original.days, original.speed
        )
    assert fake.writes == expected + [b"m,0136C4$"]

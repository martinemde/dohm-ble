"""Experimental commands use inferred syntax; fakes do not prove firmware support."""

import asyncio

import pytest
from test_client import DEVICE_ID
from test_client_commands import CommandDohm

from custom_components.dohm import client as client_module
from custom_components.dohm import protocol
from custom_components.dohm.client import (
    DohmClient,
    DohmCommandError,
    DohmError,
    DohmProtocolError,
)


class ExperimentalDohm(CommandDohm):
    def _respond(self, text):
        if text in self.replies:
            return self.replies[text]
        if text.startswith("t,"):
            return "T,opaque,clock,fields$"
        if text.startswith("I,"):
            self.device_id = text[:-1].split(",")[2]
            return "OK$"
        return super()._respond(text)


@pytest.fixture
async def experimental_pair(monkeypatch):
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.02)
    monkeypatch.setattr(client_module, "OPENING_TIMEOUT", 0.02)
    monkeypatch.setattr(client_module, "OPENING_ATTEMPTS", 1)
    fake = ExperimentalDohm()

    async def connector(_device):
        return fake

    client = DohmClient(object(), device_id=DEVICE_ID, connector=connector)
    await client.connect()
    try:
        yield client, fake
    finally:
        await client.disconnect()


def test_experimental_encoders_follow_inferred_grammar():
    assert protocol.query_time(DEVICE_ID) == b"t,0136C4$"
    assert protocol.set_device_id(DEVICE_ID, "ABCDEF") == b"I,0136C4,ABCDEF$"


async def test_clock_query_returns_uninterpreted_reply(experimental_pair):
    client, fake = experimental_pair
    fake.replies["t,0136C4$"] = ["M,1$", "T,opaque,clock,fields$"]
    assert await client.get_time() == b"T,opaque,clock,fields$"
    assert fake.writes == [b"t,0136C4$"]


async def test_clock_rejection_keeps_link(experimental_pair):
    client, fake = experimental_pair
    fake.replies["t,0136C4$"] = "Failed 03$"
    with pytest.raises(DohmCommandError):
        await client.get_time()
    assert client.is_connected


@pytest.mark.parametrize("reply", ["t,0136C4$", "T,1234", "T,1234$$"])
async def test_invalid_clock_replies_raise_protocol_error(experimental_pair, reply):
    client, fake = experimental_pair
    fake.replies["t,0136C4$"] = reply
    with pytest.raises(DohmProtocolError):
        await client.get_time()


async def test_clock_query_does_not_accept_ack(experimental_pair):
    client, fake = experimental_pair
    fake.replies["t,0136C4$"] = "OK$"
    with pytest.raises(TimeoutError):
        await client.get_time()


async def test_id_write_checks_readback_then_uses_reported_id(experimental_pair):
    client, fake = experimental_pair
    assert await client.set_device_id("ABCDEF") == "ABCDEF"
    assert client.device_id == "ABCDEF"
    await client.get_power()
    assert fake.writes == [b"I,0136C4,ABCDEF$", b"i$", b"m,ABCDEF$"]


async def test_id_write_mismatch_retains_actual_id(experimental_pair):
    client, fake = experimental_pair
    fake.replies["I,0136C4,ABCDEF$"] = "OK$"
    with pytest.raises(DohmError, match="readback mismatch"):
        await client.set_device_id("ABCDEF")
    assert client.device_id == DEVICE_ID
    assert client.is_connected


@pytest.mark.parametrize("reply", ["Failed 03$", "I,0136C4,ABCDEF$"])
async def test_id_write_failure_invalidates_cache(experimental_pair, reply):
    client, fake = experimental_pair
    fake.replies["I,0136C4,ABCDEF$"] = reply
    error = DohmCommandError if reply.startswith("Failed") else DohmProtocolError
    with pytest.raises(error):
        await client.set_device_id("ABCDEF")
    assert client.device_id is None
    assert fake.writes == [b"I,0136C4,ABCDEF$"]
    if client.is_connected:
        # connect on an already-open link retries discovery when ID is unknown.
        await client.connect()
        assert client.device_id == DEVICE_ID


async def test_id_readback_timeout_leaves_cache_unknown(experimental_pair):
    client, fake = experimental_pair
    fake.replies["i$"] = None
    with pytest.raises(TimeoutError):
        await client.set_device_id("ABCDEF")
    assert client.device_id is None
    assert fake.device_id == "ABCDEF"
    assert fake.writes == [b"I,0136C4,ABCDEF$", b"i$"]


@pytest.mark.parametrize("new_id", ["", "a,b", "a$b", "a\n", None])
async def test_id_validation_precedes_cache_invalidation(experimental_pair, new_id):
    client, fake = experimental_pair
    with pytest.raises((TypeError, ValueError)):
        await client.set_device_id(new_id)
    assert client.device_id == DEVICE_ID
    assert fake.writes == []


async def test_id_write_cancellation_invalidates_cache_and_releases_lock(
    experimental_pair,
):
    client, fake = experimental_pair
    started = asyncio.Event()
    release = asyncio.Event()
    original = fake.write_gatt_char

    async def block(char, data, response=True):
        await original(char, data, response=response)
        if data.startswith(b"I,"):
            started.set()
            await release.wait()

    fake.write_gatt_char = block
    operation = asyncio.create_task(client.set_device_id("ABCDEF"))
    await asyncio.wait_for(started.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert client.device_id is None
    assert client.is_connected
    assert await client.get_device_id() == "ABCDEF"


async def test_timeout_race_salvages_reply_behind_unrelated_frame(
    experimental_pair,
    monkeypatch,
):
    client, fake = experimental_pair
    fake.replies["m,0136C4$"] = ["S,02$", "M,1$"]

    async def miss(awaitable, _timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", miss)
    assert await client.get_power() is True
    assert client.is_connected


@pytest.mark.parametrize(
    "payload",
    [
        b"I,$",
        b"I,abc,def$",
        b"I,abc\n$",
        b"S,00$",
        b"S,11$",
        b"S,-1$",
        b"M,2$",
        b"M,0136C4,1$",
    ],
)
def test_invalid_typed_reports_are_not_accepted(payload):
    with pytest.raises(ValueError):
        protocol.parse(payload)


@pytest.mark.parametrize("device_id", ["", "a,b", "a$b", "a\n", None])
@pytest.mark.parametrize(
    "encoder",
    [
        protocol.query_power,
        protocol.query_speed,
        protocol.query_time,
        lambda device_id: protocol.set_power(device_id, True),
        lambda device_id: protocol.set_speed(device_id, 1),
    ],
)
def test_all_id_prefixed_encoders_validate_ids(encoder, device_id):
    with pytest.raises((TypeError, ValueError)):
        encoder(device_id)


@pytest.mark.parametrize("speed", [0, 11, True, 1.5, "3"])
def test_speed_encoder_validates_active_speed(speed):
    with pytest.raises((TypeError, ValueError)):
        protocol.set_speed(DEVICE_ID, speed)


@pytest.mark.parametrize("on", [0, 1, None, "0"])
def test_power_encoder_requires_bool(on):
    with pytest.raises(TypeError):
        protocol.set_power(DEVICE_ID, on)

"""Tests for DohmClient against a fake that mimics the real device replies."""

import pytest

from custom_components.dohm.client import DohmClient


class FakeDohm:
    """Stand-in BLE client that responds like the captured device."""

    def __init__(self, device_id="0136C4", speed=2, power=True):
        self.device_id = device_id
        self.speed = speed
        self.power = power
        self.writes: list[bytes] = []
        self._notify = None
        self.is_connected = True

    async def start_notify(self, _char, callback):
        self._notify = callback

    async def stop_notify(self, _char):
        self._notify = None

    async def disconnect(self):
        self.is_connected = False

    async def write_gatt_char(self, _char, data, response=True):
        self.writes.append(bytes(data))
        reply = self._respond(bytes(data).decode())
        if reply is not None and self._notify is not None:
            self._notify(0, bytearray(reply.encode()))

    def _respond(self, text: str) -> str | None:
        if text == "i$":
            return f"I,{self.device_id}$"
        body = text[:-1]  # strip terminator
        letter, _, rest = body.partition(",")
        if letter == "m":
            return f"M,{1 if self.power else 0}$"
        if letter == "s":
            return f"S,{self.speed:02d}$"
        if letter == "M":
            _id, _, value = rest.partition(",")
            self.power = value == "1"
            return "OK$"
        if letter == "S":
            _id, _, value = rest.partition(",")
            n = int(value)
            if 1 <= n <= 10:
                self.speed = n
                return "OK$"
            return "Failed 03$"
        return text  # echo unrecognized input


@pytest.fixture
def fake():
    return FakeDohm()


@pytest.fixture
async def client(fake):
    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()
    return client


async def test_connect_learns_device_id(client):
    assert client.device_id == "0136C4"


async def test_is_connected_reflects_transport(client, fake):
    assert client.is_connected is True
    await client.disconnect()
    assert client.is_connected is False


async def test_disconnect_stops_notify_before_disconnecting(client, fake):
    # BlueZ keeps a notify subscription acquired per-characteristic. If we
    # disconnect without releasing it, the next connect's start_notify fails
    # with org.bluez.Error.NotPermitted: Notify acquired.
    await client.disconnect()
    assert fake._notify is None


async def test_disconnect_still_disconnects_if_stop_notify_raises(client, fake):
    # The link may already be gone when we tear down; cleanup must not throw.
    async def boom(_char):
        raise RuntimeError("disconnected before stop_notify")

    fake.stop_notify = boom
    await client.disconnect()
    assert fake.is_connected is False


async def test_set_speed_sends_id_prefixed_command(client, fake):
    await client.set_speed(7)
    assert b"S,0136C4,7$" in fake.writes
    assert fake.speed == 7


async def test_set_speed_out_of_range_raises(client):
    with pytest.raises(ValueError):
        await client.set_speed(11)


async def test_get_speed_reflects_device(client, fake):
    fake.speed = 4
    assert await client.get_speed() == 4


async def test_set_power_off_then_on(client, fake):
    await client.set_power(False)
    assert fake.power is False
    await client.set_power(True)
    assert fake.power is True


async def test_get_power_reflects_device(client, fake):
    fake.power = False
    assert await client.get_power() is False

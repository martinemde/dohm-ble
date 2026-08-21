"""Tests for DohmClient against a fake that mimics the real device replies."""

import asyncio

import pytest
from bleak.exc import BleakDBusError, BleakError

from custom_components.dohm import client as client_module
from custom_components.dohm.client import (
    DohmClient,
    DohmCommandError,
    DohmRebondUnsupported,
)


def _notify_acquired_error() -> BleakDBusError:
    """The error BlueZ raises when a stale notify subscription lingers."""
    return BleakDBusError("org.bluez.Error.NotPermitted", ["Notify acquired"])


CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


class FakeDescriptor:
    def __init__(self, handle):
        self.handle = handle


class FakeCharacteristic:
    def __init__(self, expose_cccd=True):
        self._cccd = FakeDescriptor(0x001B) if expose_cccd else None

    def get_descriptor(self, specifier):
        if specifier == CCCD_UUID:
            return self._cccd
        return None


class FakeServices:
    def __init__(self, char):
        self._char = char

    def get_characteristic(self, _uuid):
        return self._char


class FakeDohm:
    """Stand-in BLE client that responds like the captured device."""

    def __init__(self, device_id="0136C4", speed=2, power=True,
                 expose_cccd=True, require_rearm=False, reply_delay=0.0):
        self.device_id = device_id
        self.speed = speed
        self.power = power
        self.writes: list[bytes] = []
        self.cccd_writes: list[bytes] = []
        # Ordered record of pair/unpair calls, as the ESPHome proxy backend
        # exposes them (BluetoothProxyFeature.PAIRING).
        self.bond_calls: list[str] = []
        self._notify = None
        self.is_connected = True
        self.services = FakeServices(FakeCharacteristic(expose_cccd))
        # When True, model the real device: notifications go silent until the
        # CCCD is (re)armed, and each delivered reply disarms them again.
        self._require_rearm = require_rearm
        self._armed = not require_rearm
        # Seconds to wait before delivering each reply, modelling the device's
        # slow BlueZ notification cadence (the write acks immediately; the notify
        # lags). 0.0 delivers synchronously like a snappy CoreBluetooth link.
        self._reply_delay = reply_delay

    async def start_notify(self, _char, callback):
        self._notify = callback
        self._armed = True

    async def stop_notify(self, _char):
        self._notify = None

    async def disconnect(self):
        self.is_connected = False

    async def unpair(self):
        self.bond_calls.append("unpair")

    async def pair(self):
        self.bond_calls.append("pair")

    async def write_gatt_descriptor(self, _handle, data):
        self.cccd_writes.append(bytes(data))
        if bytes(data) == b"\x01\x00":
            self._armed = True

    async def write_gatt_char(self, _char, data, response=True):
        self.writes.append(bytes(data))
        reply = self._respond(bytes(data).decode())
        if reply is None or self._notify is None or not self._armed:
            return
        if self._require_rearm:
            self._armed = False
        if self._reply_delay:
            # Deliver later, like the real device: write_gatt_char returns now
            # (ATT-acked) and the notification arrives after the delay.
            async def _deliver(cb, payload):
                await asyncio.sleep(self._reply_delay)
                cb(0, bytearray(payload.encode()))

            asyncio.ensure_future(_deliver(self._notify, reply))
        else:
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


async def test_connect_skips_identify_when_device_id_is_already_known():
    # Live: after a disconnect, i$ can stay silent for 90s while S,<id>,n$
    # still gets OK$. Requiring identify() on every connect drops that link
    # and forces pairing. A stored id must be enough to come up.
    fake = FakeDohm()
    original = fake.write_gatt_char

    async def silent_identify(char, data, response=True):
        payload = bytes(data)
        if payload == b"i$":
            fake.writes.append(payload)
            return
        await original(char, data, response=response)

    fake.write_gatt_char = silent_identify

    async def connector(_ble_device):
        return fake

    client = DohmClient(
        ble_device=object(), connector=connector, device_id="0136C4"
    )
    await client.connect()

    assert client.device_id == "0136C4"
    assert client.is_connected is True
    assert b"i$" not in fake.writes
    assert await client.get_speed() == 2


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


async def test_connect_recovers_from_stuck_notify_acquired():
    # A prior unclean disconnect (crash, dropped link, integration reload) can
    # leave BlueZ's notify subscription "acquired"; the next start_notify is then
    # refused with org.bluez.Error.NotPermitted. connect() must self-heal by
    # reconnecting and subscribing again rather than failing setup forever.
    clean = FakeDohm()
    calls = {"n": 0}

    async def connector(_ble_device):
        calls["n"] += 1
        if calls["n"] == 1:
            stuck = FakeDohm()

            async def boom(_char, _cb):
                raise _notify_acquired_error()

            stuck.start_notify = boom
            return stuck
        return clean

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()

    assert calls["n"] == 2  # reconnected once to clear the stuck subscription
    assert client.is_connected
    assert client.device_id == "0136C4"


async def test_connect_cleans_up_when_a_later_step_fails():
    # connect() gets past start_notify, then identify() sends the first command
    # and waits for a reply on a racy, single-connection link where replies can
    # time out. If that fails, connect() must release the notify subscription
    # and drop the link before propagating. Leaking a connected, notifying
    # client keeps BlueZ's notify acquired (the link stays up, so the FD is
    # never freed), and the *next* setup's start_notify is refused with
    # NotPermitted: Notify acquired -- the leak v0.1.2-0.1.4 chased downstream.
    fake = FakeDohm()

    async def no_reply(_char, _data, response=True):
        raise TimeoutError("no reply on racy link")

    fake.write_gatt_char = no_reply

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    with pytest.raises(TimeoutError):
        await client.connect()

    assert fake._notify is None  # notify subscription released
    assert fake.is_connected is False  # link dropped, BlueZ frees the acquire
    assert client.is_connected is False


async def test_connect_cleanup_failure_preserves_original_error():
    # When connect() fails partway and the cleanup disconnect itself raises (the
    # link may already be gone), the caller must still see the original cause,
    # not a confusing disconnect error.
    fake = FakeDohm()

    async def no_reply(_char, _data, response=True):
        raise TimeoutError("no reply on racy link")

    async def cleanup_boom():
        raise RuntimeError("link already gone")

    fake.write_gatt_char = no_reply
    fake.disconnect = cleanup_boom

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    with pytest.raises(TimeoutError):
        await client.connect()


async def test_connect_reraises_unrelated_notify_errors():
    async def connector(_ble_device):
        fake = FakeDohm()

        async def boom(_char, _cb):
            raise BleakError("le-connection-abort-by-local")

        fake.start_notify = boom
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    with pytest.raises(BleakError):
        await client.connect()


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


async def test_command_rearms_cccd_before_each_command(client, fake):
    # The Dohm silently stops emitting notifications on a long-lived link until
    # its CCCD is re-enabled; the official app rewrites 01 00 to the CCCD before
    # every command. Each command must do the same so replies don't go missing.
    fake.cccd_writes.clear()
    await client.get_power()
    assert fake.cccd_writes == [b"\x01\x00"]


async def test_command_succeeds_when_device_needs_rearm_each_time():
    # A device that goes notify-deaf until re-armed (and re-deafens after each
    # reply) must still answer, because the client re-arms before every command.
    fake = FakeDohm(require_rearm=True)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()  # identify() must get its reply through the re-arm
    assert client.device_id == "0136C4"
    assert await client.get_speed() == 2
    assert await client.get_power() is True


async def test_command_tolerates_missing_cccd():
    # Backends that hide the CCCD (e.g. CoreBluetooth) expose no descriptor;
    # commands must still work, relying on the start_notify subscription.
    fake = FakeDohm(expose_cccd=False)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()
    assert await client.get_speed() == 2
    assert fake.cccd_writes == []


async def test_command_survives_rearm_write_failure(client, fake):
    # A failed CCCD rewrite must not break the command: the existing
    # subscription may already be armed.
    async def boom(_handle, _data):
        raise BleakError("cannot write descriptor")

    fake.write_gatt_descriptor = boom
    assert await client.get_speed() == 2


async def test_reply_slower_than_the_old_ceiling_still_succeeds(monkeypatch):
    # Over BlueZ the device's notifications drain on a ~5s connection interval,
    # so a reply routinely lands just past the old 5.0s timeout. The wait must
    # outlast the cadence. Scaled down (timeout/delay shrunk by the same factor)
    # so the test is fast while still exercising asyncio.wait_for on a late
    # reply: delivery at 0.05s would have failed under a 0.04s ceiling.
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.20)
    fake = FakeDohm(reply_delay=0.05)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()  # identify() waits out the delayed reply
    assert client.device_id == "0136C4"
    assert await client.get_speed() == 2


async def test_command_timeout_clears_two_notify_cadences():
    # Regression guard for the shipped value: the device's BlueZ notify cadence
    # is ~5s, and a reply that misses a tick lands on the next one (~10s), so the
    # timeout must clear two cadences with margin. 5.0 (one cadence) dropped
    # replies on a coin flip; see client.COMMAND_TIMEOUT's rationale.
    assert client_module.COMMAND_TIMEOUT >= 12.0


async def test_first_command_waits_out_the_slow_opening_notify():
    # Live: i$ after start_notify replied in 14.67s, then every later command
    # in 0.00s. COMMAND_TIMEOUT is 15s, so identify lost that race, dropped the
    # link, and the next connect was notify-deaf until the top button. The first
    # command has to wait longer than that measured opening reply.
    assert client_module.FIRST_COMMAND_TIMEOUT > client_module.COMMAND_TIMEOUT
    assert client_module.FIRST_COMMAND_TIMEOUT >= 25.0
    assert client_module.IDENTIFY_ATTEMPTS >= 2


async def test_identify_retries_on_the_same_link_when_the_first_i_is_silent(
    monkeypatch,
):
    # Dropping the link after a silent first i$ is what forces a re-pair. The
    # second i$ on the same connection must be allowed to succeed.
    monkeypatch.setattr(client_module, "FIRST_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.05)
    fake = FakeDohm()
    original = fake.write_gatt_char
    seen = {"i": 0}

    async def skip_first_identify(char, data, response=True):
        payload = bytes(data)
        if payload == b"i$":
            seen["i"] += 1
            if seen["i"] == 1:
                fake.writes.append(payload)
                return
        await original(char, data, response=response)

    fake.write_gatt_char = skip_first_identify

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()

    assert seen["i"] == 2
    assert client.device_id == "0136C4"
    assert fake.is_connected is True
    assert client.is_connected is True


async def test_command_keeps_a_late_reply_that_misses_wait_for(monkeypatch):
    # wait_for can fire with the notify already queued. Treat that frame as
    # success rather than dropping the link and forcing a re-pair.
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()

    async def miss(awaitable, _timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", miss)
    assert await client.get_power() is True
    assert client.is_connected is True


async def test_unanswered_command_drops_the_link(monkeypatch):
    # The failure we could never recover from: the link stays up (is_connected
    # keeps returning True) while the device has gone notify-deaf, so every poll
    # times out against the same dead client forever and the coordinator never
    # reconnects. Modelled here by a device that re-deafens after each reply on a
    # backend that hides the CCCD, so the re-arm cannot wake it. A command that
    # goes unanswered must drop the link, so the next one rebuilds it.
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.05)
    fake = FakeDohm(require_rearm=True, expose_cccd=False)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.connect()  # identify() still gets its reply

    with pytest.raises(TimeoutError):
        await client.get_power()

    assert client.is_connected is False  # coordinator will reconnect
    assert fake.is_connected is False  # link actually dropped
    assert fake._notify is None  # notify released, no acquire leaked


async def test_transport_error_during_a_command_drops_the_link(client, fake):
    # A write that fails at the transport (half-open link, proxy dropped the
    # connection) leaves the same unrecoverable zombie as a silent device.
    async def boom(_char, _data, response=True):
        raise BleakError("le-connection-abort-by-local")

    fake.write_gatt_char = boom

    with pytest.raises(BleakError):
        await client.get_power()

    assert client.is_connected is False
    assert fake.is_connected is False


async def test_rebond_clears_the_stored_bond_then_pairs():
    # The escalation for a link that connects and acks writes but never answers:
    # the controller holds a bond key the device no longer honors. ESPHome's
    # firmware only logs the auth failure and never removes the bond, so it must
    # be cleared from this side -- unpair first, then pair, in that order.
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    outcome = await client.rebond()

    assert fake.bond_calls == ["unpair", "pair"]
    assert "paired again" in outcome


async def test_rebond_refreshes_theFakeBLEDevice():
    # Home Assistant's BLEDevice can change between connections, and by the time
    # we escalate ours is several failed polls stale. The coordinator hands in a
    # freshly resolved one, which must be what we connect with.
    seen = []

    async def connector(ble_device):
        seen.append(ble_device)
        return FakeDohm()

    fresh = object()
    client = DohmClient(ble_device=object(), connector=connector)
    await client.rebond(fresh)

    assert seen == [fresh]


async def test_rebond_drops_its_own_link():
    # rebond() runs on a short-lived connection of its own (unpair/pair are only
    # valid while connected, and the failure that got us here already tore the
    # link down). It must not leave that connection held: the device takes one
    # central at a time, so a leak here blocks the reconnect it exists to enable.
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    await client.rebond()

    assert fake.is_connected is False
    assert client.is_connected is False  # never adopted as the live client


async def test_rebond_reports_a_failed_repair_without_raising():
    # Removing a bond can itself drop the link, and the device may only grant a
    # new one in pairing mode (top button). Clearing the stale key is the half
    # that always helps, so a failed pair() is reported, not raised -- the
    # coordinator still needs to log the outcome and tell the user to press it.
    fake = FakeDohm()

    async def boom():
        fake.bond_calls.append("pair")
        raise BleakError("Pairing failed due to error: 133")

    fake.pair = boom

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    outcome = await client.rebond()

    assert fake.bond_calls == ["unpair", "pair"]
    assert "cleared the stored bond" in outcome
    assert "top button" in outcome
    assert fake.is_connected is False


async def test_rebond_unsupported_when_the_backend_has_no_unpair():
    # Host BlueZ/CoreBluetooth via plain bleak, or proxy firmware older than
    # ESPHome 2024.3.0, offer no way to clear a bond. Must fail distinguishably
    # so the coordinator reports "can't" rather than "tried and failed".
    class NoPairing(FakeDohm):
        pair = None
        unpair = None

    fake = NoPairing()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    with pytest.raises(DohmRebondUnsupported):
        await client.rebond()

    assert fake.is_connected is False  # still no leaked link


async def test_rebond_unsupported_when_unpair_is_not_implemented():
    # bleak's CoreBluetooth backend defines unpair() but raises
    # NotImplementedError; bleak_esphome does the same when the proxy firmware
    # doesn't advertise the PAIRING feature flag.
    fake = FakeDohm()

    async def not_implemented():
        raise NotImplementedError("Unpairing is not available in this version ESPHome")

    fake.unpair = not_implemented

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector)
    with pytest.raises(DohmRebondUnsupported):
        await client.rebond()

    assert fake.bond_calls == []  # never got as far as pairing
    assert fake.is_connected is False


async def test_rejected_command_keeps_the_link(client, fake):
    # A "Failed NN$" reply is a healthy, answering link -- the device just
    # refused this command. Tearing down here would reconnect on every bad
    # request for no reason.
    fake._respond = lambda _text: "Failed 05$"

    with pytest.raises(DohmCommandError):
        await client.get_power()

    assert client.is_connected is True
    assert fake.is_connected is True


# --- Connecting against a fresh device ---------------------------------------

class FakeBLEDevice:
    address = "00:22:A3:01:36:C4"

async def test_default_connector_refreshes_the_device_between_attempts(monkeypatch):
    """The BLEDevice we hold can be minutes stale by the time we reconnect.

    establish_connection retries -- essential, since the first attempt often
    comes back le-connection-abort-by-local -- but without ble_device_callback
    every retry reuses the same stale snapshot. Forwarding the callback is what
    makes the retries look for the device the way the phone app does.
    """
    import bleak_retry_connector

    calls = []

    class FakeConnected:
        def __init__(self):
            self.services = FakeServices(FakeCharacteristic())

    async def fake_establish(client_class, device, name, **kwargs):
        calls.append(kwargs.get("ble_device_callback"))
        return FakeConnected()

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)

    def refresh():
        return "fresh-device"

    client = DohmClient(ble_device=FakeBLEDevice(), ble_device_callback=refresh)
    await client._connector(client._ble_device)

    assert calls == [refresh]


async def test_default_connector_without_a_callback_still_connects(monkeypatch):
    import bleak_retry_connector

    calls = []

    class FakeConnected:
        def __init__(self):
            self.services = FakeServices(FakeCharacteristic())

    async def fake_establish(client_class, device, name, **kwargs):
        calls.append(kwargs.get("ble_device_callback"))
        return FakeConnected()

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)

    client = DohmClient(ble_device=FakeBLEDevice())
    await client._connector(client._ble_device)

    assert calls == [None]

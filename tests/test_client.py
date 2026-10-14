"""Tests for DohmClient against a fake that mimics the real device replies."""

import asyncio
from types import SimpleNamespace

import pytest
from bleak import BleakBackend
from bleak.exc import BleakDBusError, BleakError

from custom_components.dohm import client as client_module
from custom_components.dohm.client import (
    DohmClient,
    DohmCommandError,
    DohmError,
    DohmRebondUnsupported,
)


def _notify_acquired_error() -> BleakDBusError:
    """The error BlueZ raises when a stale notify subscription lingers."""
    return BleakDBusError("org.bluez.Error.NotPermitted", ["Notify acquired"])


CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# What device_id_from_address() yields for the unit the captures came from,
# MAC 00:22:A3:01:36:C4, and what FakeDohm answers to.
DEVICE_ID = "0136C4"


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

    def __init__(
        self,
        device_id="0136C4",
        speed=2,
        power=True,
        expose_cccd=True,
        require_rearm=False,
        reply_delay=0.0,
    ):
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    return client


def _mute_first(fake, count=1):
    """Make the fake swallow its first `count` replies, acking the write anyway.

    This is the live failure: the device ATT-acks immediately and the notify
    lands seconds later (14.67s measured) or not at all.
    """
    original = fake.write_gatt_char
    swallowed = {"n": 0}

    async def maybe_reply(char, data, response=True):
        if swallowed["n"] < count:
            swallowed["n"] += 1
            fake.writes.append(bytes(data))
            return
        await original(char, data, response=response)

    fake.write_gatt_char = maybe_reply
    return swallowed


async def test_connect_never_asks_the_device_for_its_id(client, fake):
    # i$ is the opening command and the slowest one there is. The id is the
    # lower three MAC bytes, so it is handed in rather than asked for, and the
    # connect path does not spend a round trip -- or a bond -- learning it.
    assert client.device_id == DEVICE_ID
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()

    assert calls["n"] == 2  # reconnected once to clear the stuck subscription
    assert client.is_connected
    assert client.device_id == "0136C4"


async def test_connect_cleans_up_when_subscribe_fails():
    # connect() must never leave a connected client behind. A held link keeps
    # BlueZ's notify subscription acquired (the link is up, so the FD is never
    # freed), and the *next* setup's start_notify is refused with NotPermitted:
    # Notify acquired -- the leak v0.1.2-0.1.4 chased downstream.
    fake = FakeDohm()

    async def boom(_char, _cb):
        raise TimeoutError("subscribe went nowhere")

    fake.start_notify = boom

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    with pytest.raises(TimeoutError):
        await client.connect()

    assert fake.is_connected is False  # link dropped, BlueZ frees the acquire
    assert client.is_connected is False


async def test_connect_cleanup_failure_preserves_original_error():
    # When connect() fails partway and the cleanup disconnect itself raises (the
    # link may already be gone), the caller must still see the original cause,
    # not a confusing disconnect error.
    fake = FakeDohm()

    async def boom(_char, _cb):
        raise TimeoutError("subscribe went nowhere")

    async def cleanup_boom():
        raise RuntimeError("link already gone")

    fake.start_notify = boom
    fake.disconnect = cleanup_boom

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    with pytest.raises(TimeoutError):
        await client.connect()


async def test_connect_reraises_unrelated_notify_errors():
    async def connector(_ble_device):
        fake = FakeDohm()

        async def boom(_char, _cb):
            raise BleakError("le-connection-abort-by-local")

        fake.start_notify = boom
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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
    # its CCCD is re-enabled; the iOS capture rewrites 01 00 before commands.
    # Keep this workaround; Android 1.0.7.0 enables CCCD only during setup.
    fake.cccd_writes.clear()
    await client.get_power()
    assert fake.cccd_writes == [b"\x01\x00"]


async def test_command_succeeds_when_device_needs_rearm_each_time():
    # A device that goes notify-deaf until re-armed (and re-deafens after each
    # reply) must still answer, because the client re-arms before every command.
    fake = FakeDohm(require_rearm=True)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    assert await client.get_speed() == 2
    assert await client.get_power() is True


async def test_command_tolerates_missing_cccd():
    # Backends that hide the CCCD (e.g. CoreBluetooth) expose no descriptor;
    # commands must still work, relying on the start_notify subscription.
    fake = FakeDohm(expose_cccd=False)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    assert await client.get_speed() == 2


async def test_command_timeout_clears_two_notify_cadences():
    # Regression guard for the shipped value: the device's BlueZ notify cadence
    # is ~5s, and a reply that misses a tick lands on the next one (~10s), so the
    # timeout must clear two cadences with margin. 5.0 (one cadence) dropped
    # replies on a coin flip; see client.COMMAND_TIMEOUT's rationale.
    assert client_module.COMMAND_TIMEOUT >= 12.0


async def test_the_opening_command_waits_out_the_slow_first_notify():
    # Live: the first reply after start_notify took 14.67s, then every command
    # after it took 0.00s. COMMAND_TIMEOUT is 15s, so the opening command lost
    # that race, dropped the link, and the next connect was notify-deaf until
    # the top button. It has to outwait that measured opening reply, and get
    # more than one try at it.
    assert client_module.OPENING_TIMEOUT > client_module.COMMAND_TIMEOUT
    assert client_module.OPENING_TIMEOUT >= 25.0
    assert client_module.OPENING_ATTEMPTS >= 2


async def test_a_slow_opening_command_keeps_the_link(monkeypatch):
    # The regression this whole change exists for. Whichever command goes first
    # on a fresh link pays the slow-first-notify cost -- it is a property of the
    # link, not of i$. Dropping the link over it is what cost us the bond, and
    # the bond only comes back with a top-button press.
    monkeypatch.setattr(client_module, "OPENING_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.05)
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    _mute_first(fake)  # the opening command goes unanswered, exactly once

    assert await client.get_power() is True  # second attempt, same connection
    assert client.is_connected is True
    assert fake.is_connected is True  # the bond survived


async def test_the_opening_grace_applies_only_to_the_opening_command(monkeypatch):
    # Once something has answered, the link has proven itself: a later silence
    # is the notify-deaf failure, and that one *must* drop the link so the next
    # poll rebuilds it. Keeping the long grace forever would strand it instead.
    monkeypatch.setattr(client_module, "OPENING_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "COMMAND_TIMEOUT", 0.05)
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    assert await client.get_power() is True  # link opens normally

    _mute_first(fake)
    with pytest.raises(TimeoutError):
        await client.get_speed()

    assert client.is_connected is False  # dropped on the first miss, no retries
    assert fake.is_connected is False


async def test_command_keeps_a_late_reply_that_misses_wait_for(monkeypatch):
    # wait_for can fire with the notify already queued. Treat that frame as
    # success rather than dropping the link and forcing a re-pair.
    fake = FakeDohm()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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
    monkeypatch.setattr(client_module, "OPENING_TIMEOUT", 0.05)
    fake = FakeDohm(require_rearm=True, expose_cccd=False)

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    await client.connect()
    assert await client.get_power() is True  # opens the link on the armed reply

    with pytest.raises(TimeoutError):
        await client.get_speed()  # device has re-deafened; re-arm cannot reach it

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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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
    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
    outcome = await client.rebond()

    assert fake.bond_calls == ["unpair", "pair"]
    assert "cleared the stored bond" in outcome
    assert "top button" in outcome
    assert fake.is_connected is False


async def test_rebond_unsupported_when_the_backend_has_no_unpair():
    # CoreBluetooth or proxy firmware older than ESPHome 2024.3.0 offers no
    # way to clear a bond. BlueZ does support unpair. Must fail distinguishably
    # so the coordinator reports "can't" rather than "tried and failed".
    class NoPairing(FakeDohm):
        pair = None
        unpair = None

    fake = NoPairing()

    async def connector(_ble_device):
        return fake

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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

    client = DohmClient(ble_device=object(), connector=connector, device_id=DEVICE_ID)
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


async def test_default_connector_forwards_the_device_callback(monkeypatch):
    """Forward the callback without claiming the dependency actually uses it.

    Audited retry-connector versions accept this argument but do not consult it
    in establish_connection. HA's wrapper manages current scanner selection.
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

    client = DohmClient(
        ble_device=FakeBLEDevice(), ble_device_callback=refresh, device_id=DEVICE_ID
    )
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

    client = DohmClient(ble_device=FakeBLEDevice(), device_id=DEVICE_ID)
    await client._connector(client._ble_device)

    assert calls == [None]


async def test_default_connector_requests_pairing(monkeypatch):
    import bleak_retry_connector

    calls = []
    fake = FakeDohm()

    async def fake_establish(client_class, device, name, **kwargs):
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)
    client = DohmClient(ble_device=FakeBLEDevice(), device_id=DEVICE_ID)
    await client.connect()

    assert calls[0]["pair"] is True
    assert fake._notify is not None


@pytest.mark.parametrize("pair", [True, False])
async def test_cache_rediscovery_preserves_pairing_request(monkeypatch, pair):
    import bleak_retry_connector

    calls = []
    incomplete = FakeDohm()
    incomplete.services = FakeServices(None)
    incomplete.cache_cleared = False

    async def clear_cache():
        incomplete.cache_cleared = True

    incomplete.clear_cache = clear_cache
    complete = FakeDohm()

    async def fake_establish(client_class, device, name, **kwargs):
        calls.append(kwargs)
        return incomplete if len(calls) == 1 else complete

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)

    def refresh():
        return FakeBLEDevice()

    result = await client_module._default_connector(
        FakeBLEDevice(), ble_device_callback=refresh, pair=pair
    )

    assert result is complete
    assert incomplete.cache_cleared
    assert not incomplete.is_connected
    assert [call["pair"] for call in calls] == [pair, pair]
    assert [call["ble_device_callback"] for call in calls] == [refresh, refresh]


async def test_rebond_default_connector_skips_pairing_until_key_is_cleared(monkeypatch):
    import bleak_retry_connector

    calls = []
    fake = FakeDohm()

    async def fake_establish(client_class, device, name, **kwargs):
        calls.append(kwargs)
        # If this were True, the stale key could fail before unpair is reached.
        assert kwargs["pair"] is False
        return fake

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)

    def refresh():
        return FakeBLEDevice()

    client = DohmClient(
        ble_device=FakeBLEDevice(), device_id=DEVICE_ID, ble_device_callback=refresh
    )
    outcome = await client.rebond()

    assert calls[0]["ble_device_callback"] is refresh
    assert fake.bond_calls == ["unpair", "pair"]
    assert "paired again" in outcome
    assert not fake.is_connected


async def test_initial_pairing_failure_does_not_report_connected(monkeypatch):
    import bleak_retry_connector

    async def fake_establish(client_class, device, name, **kwargs):
        assert kwargs["pair"] is True
        raise BleakError("Pairing failed due to error: 133")

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)
    client = DohmClient(ble_device=FakeBLEDevice(), device_id=DEVICE_ID)

    with pytest.raises(BleakError, match="Pairing failed"):
        await client.connect()

    assert not client.is_connected
    assert not client._notifying


async def test_initial_pairing_unsupported_gives_actionable_error(monkeypatch):
    import bleak_retry_connector

    async def fake_establish(client_class, device, name, **kwargs):
        assert kwargs["pair"] is True
        raise NotImplementedError("Pairing is not available in this version ESPHome")

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)
    client = DohmClient(ble_device=FakeBLEDevice(), device_id=DEVICE_ID)

    with pytest.raises(DohmError, match="2024.3.0 or newer") as exc:
        await client.connect()

    assert isinstance(exc.value.__cause__, NotImplementedError)
    assert not client.is_connected
    assert not client._notifying


async def test_unpaired_recovery_preserves_unsupported_connect_error(monkeypatch):
    import bleak_retry_connector

    async def fake_establish(client_class, device, name, **kwargs):
        assert kwargs["pair"] is False
        raise NotImplementedError("unrelated backend connection feature")

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", fake_establish)
    client = DohmClient(ble_device=FakeBLEDevice(), device_id=DEVICE_ID)

    with pytest.raises(NotImplementedError, match="unrelated backend"):
        await client.rebond()

    assert not client.is_connected


# --- Recovery when unpair removes the device (BlueZ) -------------------------


class FakeBlueZ(FakeDohm):
    """Model RemoveDevice clearing the path required by pair()."""

    backend_id = BleakBackend.BLUEZ_DBUS

    def __init__(self):
        super().__init__()
        self.path = "/org/bluez/hci0/dev_00_22_A3_01_36_C4"
        self._backend = SimpleNamespace(_device_path=self.path)
        self.events = []

    async def unpair(self):
        self.events.append("unpair")
        self.bond_calls.append("unpair")
        self._backend._device_path = None
        self.is_connected = False

    async def connect(self):
        self.events.append("connect/discover")
        self._backend._device_path = self.path
        self.is_connected = True

    async def pair(self):
        assert self.is_connected and self._backend._device_path is not None
        self.events.append("pair")
        self.bond_calls.append("pair")

    async def disconnect(self):
        self.events.append("disconnect")
        await super().disconnect()


def bluez_client(fake):
    async def connector(_device):
        return fake

    return DohmClient(FakeBLEDevice(), device_id=DEVICE_ID, connector=connector)


async def test_bluez_rebond_rediscovers_before_pairing():
    fake = FakeBlueZ()
    client = bluez_client(fake)

    outcome = await client.rebond()

    assert outcome == "cleared the stored bond and paired again"
    assert fake.events == ["unpair", "connect/discover", "pair", "disconnect"]
    assert fake.bond_calls == ["unpair", "pair"]
    assert not fake.is_connected
    assert not client.is_connected
    assert not client._recovering


async def test_rebond_still_connected_proxy_needs_no_reconnect():
    fake = FakeDohm()

    async def must_not_reconnect():
        raise AssertionError("proxy should pair on its existing link")

    fake.connect = must_not_reconnect
    outcome = await bluez_client(fake).rebond()

    assert "paired again" in outcome
    assert fake.bond_calls == ["unpair", "pair"]
    assert not fake.is_connected


async def test_rebond_refuses_unknown_controller_after_unpair_disconnects():
    fake = FakeBlueZ()
    del fake._backend

    async def unpair():
        fake.bond_calls.append("unpair")
        fake.is_connected = False

    fake.unpair = unpair
    outcome = await bluez_client(fake).rebond()

    assert "cannot verify the original Bluetooth controller" in outcome
    assert "cleared the stored bond" in outcome
    assert fake.bond_calls == ["unpair"]
    assert fake.events == ["disconnect"]


@pytest.mark.parametrize("changed", ["other_adapter", "lost_identity"])
async def test_bluez_rebond_does_not_pair_if_controller_changes(changed):
    fake = FakeBlueZ()

    async def reconnect():
        fake.events.append("connect/discover")
        fake._backend._device_path = (
            "/org/bluez/hci1/dev_00_22_A3_01_36_C4"
            if changed == "other_adapter"
            else None
        )
        fake.is_connected = True

    fake.connect = reconnect
    outcome = await bluez_client(fake).rebond()

    assert "controller changed" in outcome
    assert "no pairing requested" in outcome
    assert fake.bond_calls == ["unpair"]  # never unpair/pair a second controller
    assert not fake.is_connected


async def test_ha_rebond_uses_actual_scanner_not_requested_device_source():
    fake = FakeBlueZ()
    fake._connected_scanner = SimpleNamespace(source="local-adapter")

    async def reconnect():
        fake.events.append("connect/discover")
        # HA is allowed to re-route at connect time, ignoring requested source.
        fake._connected_scanner = SimpleNamespace(source="bedroom-proxy")
        fake.is_connected = True

    fake.connect = reconnect
    outcome = await bluez_client(fake).rebond()

    assert "controller changed" in outcome
    assert fake.bond_calls == ["unpair"]
    assert not fake.is_connected


async def test_ha_rebond_can_pair_after_reconnect_to_same_scanner():
    fake = FakeBlueZ()
    fake._connected_scanner = SimpleNamespace(source="local-adapter")
    client = bluez_client(fake)

    outcome = await client.rebond()

    assert "paired again" in outcome
    assert fake.bond_calls == ["unpair", "pair"]
    assert not fake.is_connected


async def test_rebond_reconnect_failure_reports_partial_success():
    fake = FakeBlueZ()

    async def failure():
        fake.events.append("connect/discover")
        raise BleakError("device no longer advertising")

    fake.connect = failure
    client = bluez_client(fake)
    outcome = await client.rebond()

    assert "cleared the stored bond; re-pair failed" in outcome
    assert "device no longer advertising" in outcome
    assert fake.bond_calls == ["unpair"]
    assert not fake.is_connected
    assert not client._recovering


async def test_rebond_reconnect_must_really_connect():
    fake = FakeBlueZ()

    async def no_link():
        pass

    fake.connect = no_link
    outcome = await bluez_client(fake).rebond()

    assert "did not establish a link" in outcome
    assert fake.bond_calls == ["unpair"]
    assert not fake.is_connected


@pytest.mark.parametrize("stage", ["connect", "pair"])
async def test_rebond_has_bounded_reconnect_and_pair_waits(monkeypatch, stage):
    fake = FakeBlueZ()

    async def forever():
        await asyncio.Event().wait()

    setattr(fake, stage, forever)
    monkeypatch.setattr(client_module, "REBOND_RECONNECT_TIMEOUT", 0.001)
    monkeypatch.setattr(client_module, "REBOND_PAIR_TIMEOUT", 0.001)
    client = bluez_client(fake)
    outcome = await client.rebond()

    assert "cleared the stored bond; re-pair failed" in outcome
    assert "TimeoutError" in outcome
    assert not fake.is_connected
    assert not client._recovering


@pytest.mark.parametrize("stage", ["connect", "pair"])
async def test_rebond_cancellation_cleans_up_and_releases_recovery_state(stage):
    fake = FakeBlueZ()
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    setattr(fake, stage, blocked)
    client = bluez_client(fake)
    task = asyncio.create_task(client.rebond())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not fake.is_connected
    assert not client._recovering
    assert not client._lock.locked()


async def test_recovery_blocks_normal_connections_commands_and_second_rebond():
    fake = FakeBlueZ()
    started = asyncio.Event()
    resume = asyncio.Event()
    real_pair = fake.pair

    async def blocked_pair():
        started.set()
        await resume.wait()
        await real_pair()

    fake.pair = blocked_pair
    client = bluez_client(fake)
    task = asyncio.create_task(client.rebond())
    await started.wait()
    try:
        with pytest.raises(DohmError, match="in progress"):
            await client.connect()
        with pytest.raises(DohmError, match="in progress"):
            await client.get_power()
        with pytest.raises(DohmError, match="in progress"):
            await client.rebond()
    finally:
        resume.set()
        await task

    assert fake.bond_calls == ["unpair", "pair"]
    assert not client._recovering


async def test_default_bluez_connector_explicitly_checks_pairing_on_existing_link(
    monkeypatch,
):
    import bleak_retry_connector

    fake = FakeBlueZ()

    async def establish(*args, **kwargs):
        assert kwargs["pair"] is True
        return fake  # connect's Pair step may have been skipped on this live link

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish)
    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID)
    await client.connect()

    assert fake.bond_calls == ["pair"]
    assert client.is_connected
    await client.disconnect()


async def test_default_bluez_pair_failure_drops_transport(monkeypatch):
    import bleak_retry_connector

    fake = FakeBlueZ()

    async def establish(*args, **kwargs):
        return fake

    async def fail():
        raise BleakError("authentication failed")

    fake.pair = fail
    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish)
    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID)
    with pytest.raises(BleakError, match="authentication failed"):
        await client.connect()

    assert not fake.is_connected
    assert not client.is_connected


async def test_rebond_unpair_failure_is_not_reported_as_key_cleared():
    fake = FakeBlueZ()

    async def fail():
        raise BleakError("RemoveDevice failed")

    fake.unpair = fail
    client = bluez_client(fake)
    with pytest.raises(BleakError, match="RemoveDevice failed"):
        await client.rebond()

    assert fake.bond_calls == []
    assert not fake.is_connected
    assert not client._recovering


async def test_bluez_rebond_retries_transient_reconnect_errors(monkeypatch):
    import bleak_retry_connector

    monkeypatch.setattr(bleak_retry_connector, "calculate_backoff_time", lambda _: 0)
    fake = FakeBlueZ()
    real_connect = fake.connect
    calls = 0

    async def racy_connect():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BleakError("le-connection-abort-by-local")
        await real_connect()

    fake.connect = racy_connect
    outcome = await bluez_client(fake).rebond()

    assert calls == 2
    assert "paired again" in outcome
    assert fake.bond_calls == ["unpair", "pair"]
    assert not fake.is_connected


async def test_rebond_waits_for_inflight_command_before_disconnect():
    fake = FakeBlueZ()
    client = bluez_client(fake)
    await client.connect()
    original_command = client.get_power
    started = asyncio.Event()
    resume = asyncio.Event()
    original_write = fake.write_gatt_char

    async def blocked_write(*args, **kwargs):
        started.set()
        await resume.wait()
        await original_write(*args, **kwargs)

    fake.write_gatt_char = blocked_write
    command = asyncio.create_task(original_command())
    await started.wait()
    recovery = asyncio.create_task(client.rebond())
    # Let recovery announce itself and wait for the command lock.
    await asyncio.sleep(0)
    assert fake.is_connected
    assert fake.events == []
    resume.set()
    assert await command is True
    assert "paired again" in await recovery
    assert fake.events[0] == "disconnect"
    assert not client.is_connected


async def test_deliberate_repair_can_fix_the_newly_selected_proxy(monkeypatch):
    import bleak_retry_connector

    proxy = FakeDohm()
    proxy._connected_scanner = SimpleNamespace(source="new-bedroom-proxy")
    keys = {"old-proxy": "valid-old-key", "new-bedroom-proxy": "stale-key"}

    async def unpair():
        proxy.bond_calls.append("unpair")
        keys.pop(proxy._connected_scanner.source, None)

    async def pair():
        proxy.bond_calls.append("pair")
        keys[proxy._connected_scanner.source] = "new-key"

    async def establish(*args, **kwargs):
        assert kwargs["pair"] is False  # clearing the stale key comes first
        return proxy

    proxy.unpair = unpair
    proxy.pair = pair
    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish)
    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID)
    outcome = await client.rebond()

    assert "paired again" in outcome
    assert keys == {"old-proxy": "valid-old-key", "new-bedroom-proxy": "new-key"}
    assert proxy.bond_calls == ["unpair", "pair"]
    assert not proxy.is_connected


async def test_failed_normal_connection_never_clears_a_proxy_key(monkeypatch):
    import bleak_retry_connector

    proxy = FakeDohm()

    async def establish(*args, **kwargs):
        assert kwargs["pair"] is True
        raise BleakError("authentication failed on new proxy")

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish)
    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID)
    with pytest.raises(BleakError, match="authentication failed"):
        await client.connect()

    assert proxy.bond_calls == []
    assert not client.is_connected


@pytest.mark.parametrize(
    "path",
    ["/org/bluez/not_an_adapter/dev_00_22_A3_01_36_C4", "/org/bluez/hci0/dev_bad", ""],
)
def test_controller_guard_rejects_malformed_native_paths(path):
    fake = FakeBlueZ()
    fake._backend._device_path = path
    assert client_module._connection_controller(fake) is None


async def test_rebond_cleanup_is_bounded_and_preserves_pair_outcome(
    monkeypatch, caplog
):
    fake = FakeBlueZ()
    called = asyncio.Event()

    async def hung_disconnect():
        called.set()
        await asyncio.Event().wait()

    fake.disconnect = hung_disconnect
    monkeypatch.setattr(client_module, "RECOVERY_CLEANUP_TIMEOUT", 0.001)
    client = bluez_client(fake)
    with caplog.at_level("DEBUG", logger=client_module.__name__):
        outcome = await client.rebond()

    assert called.is_set()
    assert "paired again" in outcome
    assert "cleanup after pairing recovery" in caplog.text
    assert not client._recovering
    assert not client.is_connected


async def test_disconnect_still_drops_transport_when_stop_notify_hangs(
    client, fake, monkeypatch
):
    async def forever(*args):
        await asyncio.Event().wait()

    fake.stop_notify = forever
    monkeypatch.setattr(client_module, "RECOVERY_CLEANUP_TIMEOUT", 0.001)
    await client.disconnect()
    assert not fake.is_connected
    assert not client._notifying


async def test_disconnect_cancellation_during_stop_notify_releases_transport(
    client, fake
):
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    fake.stop_notify = blocked
    task = asyncio.create_task(client.disconnect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fake.is_connected
    assert not client._notifying


async def test_recovery_waits_for_connection_already_in_progress():
    first = FakeBlueZ()
    recovery_client = FakeBlueZ()
    started = asyncio.Event()
    resume = asyncio.Event()
    calls = 0

    async def connector(_device):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await resume.wait()
            return first
        # Recovery cannot acquire a second link while connect() owns the first.
        assert not first.is_connected
        return recovery_client

    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID, connector=connector)
    connecting = asyncio.create_task(client.connect())
    await started.wait()
    recovering = asyncio.create_task(client.rebond())
    await asyncio.sleep(0)
    assert calls == 1
    assert recovery_client.events == []
    resume.set()
    await connecting
    assert "paired again" in await recovering
    assert calls == 2
    assert not first.is_connected
    assert not recovery_client.is_connected
    assert not client.is_connected
    assert not client._recovering


async def test_default_bluez_explicit_pair_has_timeout_and_cleans_up(monkeypatch):
    import bleak_retry_connector

    fake = FakeBlueZ()

    async def establish(*args, **kwargs):
        return fake

    async def forever():
        await asyncio.Event().wait()

    fake.pair = forever
    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish)
    monkeypatch.setattr(client_module, "REBOND_PAIR_TIMEOUT", 0.001)
    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID)
    with pytest.raises(TimeoutError):
        await client.connect()
    assert not fake.is_connected
    assert not client.is_connected


async def test_concurrent_connections_do_not_replace_a_live_client():
    fake = FakeBlueZ()
    started = asyncio.Event()
    resume = asyncio.Event()
    calls = 0

    async def connector(_device):
        nonlocal calls
        calls += 1
        started.set()
        await resume.wait()
        return fake

    client = DohmClient(FakeBLEDevice(), device_id=DEVICE_ID, connector=connector)
    first = asyncio.create_task(client.connect())
    await started.wait()
    second = asyncio.create_task(client.connect())
    await asyncio.sleep(0)
    resume.set()
    await asyncio.gather(first, second)
    assert calls == 1
    assert client.is_connected
    await client.disconnect()

"""Async BLE client for the Marpac Dohm.

Wraps the pure protocol layer with a connection and a request/response loop.
Connections go through ``bleak-retry-connector``'s ``establish_connection``,
which works both standalone and inside Home Assistant's Bluetooth stack
(including ESPHome proxies). A ``connector`` can be injected for testing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from functools import partial

from bleak import BleakBackend
from bleak.exc import BleakError

from . import protocol
from .const import CHARACTERISTIC_UUID, MAX_SPEED, MIN_SPEED

_LOGGER = logging.getLogger(__name__)

# Replies can lag well behind the write that triggered them. The write itself
# is ATT-acked in tens of ms, but the device requests a slow LE connection
# interval after connecting (power saving), so over BlueZ its notifications only
# drain every ~5s -- CoreBluetooth keeps the link snappy, BlueZ honors the slow
# interval. A reply that just misses a tick lands on the next one, so the wait
# must clear two cadences with margin; 5s (== one cadence) drops replies on a
# coin flip. Still well under the coordinator's 30s poll, so a truly dead device
# is reported within one cycle.
COMMAND_TIMEOUT = 15.0

# The *opening* command on a link is in a different league from the rest. On a
# freshly paired CoreBluetooth link we measured 14.67s for the first reply then
# 0.00s for every command after it. COMMAND_TIMEOUT is 15s, so the opening
# command loses that race on a coin flip -- and dropping the link over it is
# what used to cost us the bond, because the device grants a new one only in
# pairing mode (top button, ~5s). This is a property of the link, not of any
# one command: whichever command happens to go first pays it. So the opening
# command gets a longer wait and repeated tries on the *same* connection, and
# only a link that stays mute through all of them is torn down.
OPENING_TIMEOUT = 30.0
OPENING_ATTEMPTS = 3

# Bound reconnect after a deliberate unpair; the machine may have left pairing
# mode. This is separate from the long wait for an opening notification.
REBOND_RECONNECT_TIMEOUT = 30.0
REBOND_PAIR_TIMEOUT = 30.0
RECOVERY_CLEANUP_TIMEOUT = 5.0

# Client Characteristic Configuration Descriptor (notify enable bit).
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


def _is_notify_acquired(err: BleakError) -> bool:
    """True for ``org.bluez.Error.NotPermitted: Notify acquired``.

    BlueZ refuses ``StartNotify`` with this when an earlier connection's notify
    subscription is still acquired (e.g. an unclean disconnect never released
    it). Matched by message so it survives bleak/BlueZ version differences.
    """
    text = str(err)
    return "Notify acquired" in text or "NotPermitted" in text


def _connection_controller(client) -> tuple[str, str] | None:
    """Identify the actual controller, not the requested BLEDevice.

    HA chooses its transport at connect time, ignoring the supplied device's
    source. No public source-pin API is available in the audited stack. These
    narrow introspection points are used only as a safety guard: if they stop
    exposing identity, disconnected recovery fails closed rather than pairing
    a different controller. They do not override HA routing.
    """
    scanner = getattr(client, "_connected_scanner", None)
    source = getattr(scanner, "source", None)
    if isinstance(source, str) and source:
        return ("ha", source)
    backend = getattr(client, "_backend", None)
    path = getattr(backend, "_device_path", None)
    if isinstance(path, str):
        match = re.fullmatch(
            r"(/org/bluez/hci[0-9]+)/dev_(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}", path
        )
        if match:
            return ("bluez", match[1])
    return None


class DohmError(Exception):
    """Base error for Dohm operations."""


class DohmCommandError(DohmError):
    """The device rejected a command (replied ``Failed NN$``)."""


class DohmRebondUnsupported(DohmError):
    """The Bluetooth backend can't clear a stored bond.

    Raised for CoreBluetooth (macOS has no unpair API) and for ESPHome
    firmware older than 2024.3.0, which doesn't advertise the ``PAIRING``
    feature flag. BlueZ supports unpairing, but it also disconnects the device.
    """


async def _default_connector(
    ble_device, *, ble_device_callback=None, pair: bool = True
):
    """Connect and request pairing, retrying against a fresh BLEDevice.

    Pairing is awaited by backends that support ``pair=True`` before commands
    run. Recovery uses ``pair=False`` so stale keys can be cleared first.
    CoreBluetooth initiates pairing implicitly on authenticated GATT access.

    Connecting is racy on this device -- the first attempt often comes back
    ``le-connection-abort-by-local`` -- so ``establish_connection`` retrying is
    essential. The synchronous ``ble_device_callback`` is forwarded for API
    compatibility, not treated as a rediscovery guarantee: the audited retry
    connector accepts it without using it. HA's client wrapper itself chooses
    from its current scanners on each connect attempt.
    """
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    async def establish():
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                ble_device_callback=ble_device_callback,
                pair=pair,
            )
            # BlueZ connect(pair=True) skips Pair if already connected. Explicit
            # pair() checks the bond even in that case, and preserves valid keys.
            if pair and getattr(client, "backend_id", None) == BleakBackend.BLUEZ_DBUS:
                try:
                    async with asyncio.timeout(REBOND_PAIR_TIMEOUT):
                        await client.pair()
                except BaseException:
                    try:
                        async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                            await client.disconnect()
                    except Exception as err:  # noqa: BLE001 - preserve pair failure
                        _LOGGER.debug("cleanup after pairing failure: %s", err)
                    raise
            return client
        except NotImplementedError as err:
            if not pair:
                raise
            raise DohmError(
                "Bluetooth backend cannot request pairing; update ESPHome proxy "
                "firmware to 2024.3.0 or newer (PAIRING support) or use a "
                "supported adapter"
            ) from err

    client = await establish()
    # A connection cut short (e.g. the device's brief connectable window) can
    # leave a partial service cache that hides the command characteristic. If
    # it's missing, clear the cache and rediscover once.
    if client.services.get_characteristic(CHARACTERISTIC_UUID) is None:
        await client.clear_cache()
        await client.disconnect()
        client = await establish()
    return client


class DohmClient:
    def __init__(
        self,
        ble_device,
        *,
        device_id: str,
        connector: Callable[[object], Awaitable[object]] | None = None,
        ble_device_callback: Callable[[], object] | None = None,
    ) -> None:
        self._ble_device = ble_device
        # Bound into the default connector rather than passed through the
        # connector signature, so an injected test connector stays one-argument.
        self._connector = connector or partial(
            _default_connector, ble_device_callback=ble_device_callback, pair=True
        )
        # Clearing a stale key must not depend on authenticating with that key.
        # Injected connectors retain the same one-argument interface in both paths.
        self._rebond_connector = connector or partial(
            _default_connector, ble_device_callback=ble_device_callback, pair=False
        )
        self._client = None
        self._notifying = False
        # Derived from the address, not asked for over the wire: it is the lower
        # three MAC bytes and the device cannot change it. Asking meant i$ as the
        # opening command, which is the slowest and least reliable one there is.
        self._device_id = device_id
        # Has anything on this link actually answered yet? Until it has, every
        # command is the opening command and gets OPENING_TIMEOUT.
        self._opened = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._recovering = False

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, ble_device=None) -> None:
        if self._recovering:
            raise DohmError("pairing recovery is in progress")
        async with self._lock:
            if self._recovering:
                raise DohmError("pairing recovery is in progress")
            if self.is_connected:
                return
            await self._connect(ble_device)

    async def _connect(self, ble_device=None) -> None:
        # In Home Assistant the BLEDevice can change between connections, so
        # allow refreshing it on (re)connect.
        if ble_device is not None:
            self._ble_device = ble_device
        _LOGGER.debug("connecting to %s", getattr(self._ble_device, "address", "?"))
        self._client = await self._connector(self._ble_device)
        # Once connected, any later failure must not leak a connected, notifying
        # client: the held link keeps BlueZ's notify subscription acquired, so
        # the next start_notify is refused with NotPermitted: Notify acquired.
        # Release it before propagating. _drop() is best-effort cleanup.
        try:
            self._opened = False
            await self._subscribe()
            _LOGGER.debug("connected; device id %s", self._device_id)
        except BaseException:
            await self._drop()
            raise

    async def _subscribe(self) -> None:
        try:
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        except BleakError as err:
            if not _is_notify_acquired(err):
                raise
            # An earlier connection left BlueZ's notify subscription acquired and
            # our graceful disconnect never ran to release it (crash, dropped
            # link, integration reload). A full disconnect drops the device
            # connection and frees it; reconnect once and subscribe again.
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception as err:  # noqa: BLE001 - best-effort; link may be gone
                _LOGGER.debug("stop notify before recovery failed: %s", err)
            await self._client.disconnect()
            self._client = await self._connector(self._ble_device)
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        self._notifying = True

    async def _drop(self) -> None:
        """Best-effort teardown. The caller already has the failure that matters."""
        try:
            await self.disconnect()
        except Exception as err:  # noqa: BLE001 - best-effort; keep the real cause
            _LOGGER.debug("transport cleanup failed: %s", err)

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._opened = False
        if client is None:
            return
        _LOGGER.debug("disconnecting (notifying=%s)", self._notifying)
        # Release BlueZ's per-characteristic notify subscription before dropping
        # the link. Skipping this leaves it "acquired", so the next connect's
        # start_notify fails with org.bluez.Error.NotPermitted: Notify acquired,
        # piling up HCI resources until ENOMEM. The link may already be gone, so
        # failing to stop notify must not prevent the disconnect.
        try:
            if self._notifying:
                try:
                    async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                        await client.stop_notify(CHARACTERISTIC_UUID)
                except Exception as err:  # noqa: BLE001 - best-effort cleanup
                    _LOGGER.debug("stop notify during disconnect failed: %s", err)
                finally:
                    self._notifying = False
        finally:
            # Cancellation during stop_notify must still release the single
            # connection slot. Bound this separately from notification cleanup.
            async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                await client.disconnect()

    async def rebond(self, ble_device=None) -> str:
        """Deliberately clear one controller's bond and request a new one.

        Silence alone is not proof of a stale key. Only a user's recovery action
        calls this. ESPHome can pair on the still-connected link; BlueZ removes
        its device object during unpair and must connect/discover again first.
        A reconnect may route elsewhere in HA, so verify the actual controller
        before pairing. Never clear a second controller automatically.
        """
        if self._recovering:
            raise DohmError("pairing recovery is already in progress")
        self._recovering = True
        try:
            async with self._lock:
                await self.disconnect()
                if ble_device is not None:
                    self._ble_device = ble_device
                return await self._rebond()
        finally:
            self._recovering = False

    async def _rebond(self) -> str:
        client = await self._rebond_connector(self._ble_device)
        try:
            unpair = getattr(client, "unpair", None)
            pair = getattr(client, "pair", None)
            if unpair is None or pair is None:
                raise DohmRebondUnsupported(
                    f"{type(client).__name__} exposes no unpair/pair"
                )
            controller = _connection_controller(client)
            _LOGGER.debug("pairing recovery selected controller %s", controller)
            try:
                await unpair()
            except NotImplementedError as err:
                raise DohmRebondUnsupported(str(err) or type(client).__name__) from err
            try:
                if not client.is_connected:
                    if controller is None:
                        raise DohmError(
                            "cannot verify the original Bluetooth controller; "
                            "retry normal setup after rediscovery"
                        )
                    # Reuse the public client, whose native BlueZ backend reset
                    # its device path in unpair(). connect() performs discovery
                    # again. Its recovery constructor still has pair=False.
                    from bleak_retry_connector import retry_bluetooth_connection_error

                    reconnect = retry_bluetooth_connection_error(attempts=3)(
                        client.connect
                    )
                    async with asyncio.timeout(REBOND_RECONNECT_TIMEOUT):
                        await reconnect()
                    if not client.is_connected:
                        raise DohmError("Bluetooth reconnect did not establish a link")
                    reconnected_controller = _connection_controller(client)
                    if reconnected_controller != controller:
                        raise DohmError(
                            f"Bluetooth controller changed during recovery "
                            f"({controller!r} -> {reconnected_controller!r}); "
                            "no pairing requested on the replacement controller"
                        )
                # Like Android's saved-device path: connect, then request a bond.
                # Use a bound for proxies that never return a pairing response.
                async with asyncio.timeout(REBOND_PAIR_TIMEOUT):
                    await pair()
            except Exception as err:  # noqa: BLE001 - report partial recovery
                return (
                    f"cleared the stored bond; re-pair failed "
                    f"({type(err).__name__}: {err}) -- hold the top button ~5s"
                )
            return "cleared the stored bond and paired again"
        finally:
            try:
                async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                    await client.disconnect()
            except Exception as err:  # noqa: BLE001 - preserve recovery outcome
                _LOGGER.debug("cleanup after pairing recovery: %s", err)

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def _rearm_notify(self) -> None:
        # The Dohm goes notify-deaf on a long-lived link until its CCCD is
        # re-enabled: the device stops emitting notifications even though writes
        # are still ATT-acked, so command replies silently go missing. The
        # iOS capture rewrites 01 00 to the CCCD before commands; retain that
        # workaround. Android 1.0.7.0 writes it only during connection setup.
        # Best-effort -- some backends (e.g. CoreBluetooth) hide the CCCD,
        # where the start_notify subscription is the only handle we have.
        char = self._client.services.get_characteristic(CHARACTERISTIC_UUID)
        cccd = char.get_descriptor(CCCD_UUID) if char is not None else None
        if cccd is None:
            # Worth logging every time: if the backend in use hides the CCCD,
            # the v0.1.6 re-arm is silently a no-op and cannot be what keeps
            # replies flowing (or what fails to).
            _LOGGER.debug("no CCCD exposed for %s; re-arm skipped", CHARACTERISTIC_UUID)
            return
        try:
            await self._client.write_gatt_descriptor(cccd.handle, b"\x01\x00")
        except Exception as err:  # noqa: BLE001 - best-effort; may already be armed
            _LOGGER.debug("CCCD re-arm failed on handle 0x%04x: %s", cccd.handle, err)

    async def _command(self, payload: bytes):
        """Send a command and return its parsed reply."""
        if self._recovering:
            raise DohmError("pairing recovery is in progress")
        async with self._lock:
            if self._recovering:
                raise DohmError("pairing recovery is in progress")
            raw = await self._exchange(payload)
        message = protocol.parse(raw)
        if isinstance(message, protocol.Failure):
            raise DohmCommandError(f"device rejected {payload!r}: {message.code}")
        return message

    async def _exchange(self, payload: bytes) -> bytes:
        """One request/reply, with the opening command handled as its own case."""
        if self._opened:
            return await self._send(payload, COMMAND_TIMEOUT, drop_on_failure=True)

        last_err: TimeoutError | None = None
        for attempt in range(1, OPENING_ATTEMPTS + 1):
            try:
                raw = await self._send(payload, OPENING_TIMEOUT, drop_on_failure=False)
            except TimeoutError as err:
                # Silence on a fresh link is not evidence the link is bad; it is
                # what a slow first notify looks like. Ask again on the same
                # connection rather than dropping a bond we may never get back.
                last_err = err
                _LOGGER.debug(
                    "opening command %r unanswered (attempt %d/%d)",
                    payload,
                    attempt,
                    OPENING_ATTEMPTS,
                )
            except Exception:
                # Not a timeout: the link itself is bad, so stop retrying on it.
                # Cancellation is not caught here -- it says nothing about the
                # link, and tearing one down over it would be wrong.
                await self._drop()
                raise
            else:
                self._opened = True
                return raw

        # Mute through every attempt: connected, ATT-acking, and notify-deaf.
        # Nothing here reconnects on its own, so the link has to go or the
        # coordinator retries this same dead client every poll, forever.
        _LOGGER.debug("link never answered %r; dropping it", payload)
        await self._drop()
        raise last_err

    async def _send(
        self, payload: bytes, timeout: float, *, drop_on_failure: bool
    ) -> bytes:
        await self._rearm_notify()
        while not self._queue.empty():
            self._queue.get_nowait()
        started = time.monotonic()
        try:
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID, payload, response=True
            )
            acked = time.monotonic()
            try:
                raw = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError:
                # wait_for can give up with the notify already queued. Taking
                # that frame costs nothing; discarding it costs a reconnect.
                if self._queue.empty():
                    raise
                raw = self._queue.get_nowait()
            # The ack/reply split is the diagnostic that matters: a device that
            # acks fast and then never answers has gone notify-deaf, while a
            # slow ack means the link itself is struggling.
            _LOGGER.debug(
                "%r -> %r (ack %.2fs, reply %.2fs)",
                payload,
                bytes(raw),
                acked - started,
                time.monotonic() - acked,
            )
        except Exception as err:
            # Cancellation is deliberately not in here: it is not evidence of a
            # bad link, and dropping one over it would cost a reconnect.
            _LOGGER.debug(
                "%r failed after %.2fs (%s: %s)",
                payload,
                time.monotonic() - started,
                type(err).__name__,
                err,
            )
            if drop_on_failure:
                await self._drop()
            raise
        return raw

    async def set_speed(self, speed: int) -> None:
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be {MIN_SPEED}..{MAX_SPEED}, got {speed}")
        await self._command(protocol.set_speed(self._device_id, speed))

    async def get_speed(self) -> int:
        return (await self._command(protocol.query_speed(self._device_id))).speed

    async def set_power(self, on: bool) -> None:
        await self._command(protocol.set_power(self._device_id, on))

    async def get_power(self) -> bool:
        return (await self._command(protocol.query_power(self._device_id))).on

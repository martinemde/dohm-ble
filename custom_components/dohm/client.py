"""Async BLE client for the Marpac Dohm.

Wraps the pure protocol layer with a connection and a request/response loop.
Connections go through ``bleak-retry-connector``'s ``establish_connection``,
which works both standalone and inside Home Assistant's Bluetooth stack
(including ESPHome proxies). A ``connector`` can be injected for testing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

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


class DohmError(Exception):
    """Base error for Dohm operations."""


class DohmCommandError(DohmError):
    """The device rejected a command (replied ``Failed NN$``)."""


async def _default_connector(ble_device):
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    client = await establish_connection(
        BleakClientWithServiceCache, ble_device, ble_device.address
    )
    # A connection cut short (e.g. the device's brief connectable window) can
    # leave a partial service cache that hides the command characteristic. If
    # it's missing, clear the cache and rediscover once.
    if client.services.get_characteristic(CHARACTERISTIC_UUID) is None:
        await client.clear_cache()
        await client.disconnect()
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, ble_device.address
        )
    return client


class DohmClient:
    def __init__(
        self,
        ble_device,
        *,
        connector: Callable[[object], Awaitable[object]] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._connector = connector or _default_connector
        self._client = None
        self._notifying = False
        self._device_id: str | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()

    @property
    def device_id(self) -> str | None:
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, ble_device=None) -> None:
        # In Home Assistant the BLEDevice can change between connections, so
        # allow refreshing it on (re)connect.
        if ble_device is not None:
            self._ble_device = ble_device
        _LOGGER.debug("connecting to %s", getattr(self._ble_device, "address", "?"))
        self._client = await self._connector(self._ble_device)
        # Once connected, any later failure (notably identify() timing out on
        # this racy single-connection link) must not leak a connected, notifying
        # client: the held link keeps BlueZ's notify subscription acquired, so
        # the next start_notify is refused with NotPermitted: Notify acquired.
        # Release it before propagating. disconnect() is best-effort cleanup.
        try:
            await self._subscribe()
            await self.identify()
            _LOGGER.debug("connected; device id %s", self._device_id)
        except BaseException:
            try:
                await self.disconnect()
            except Exception:  # noqa: BLE001 - best-effort; keep the real cause
                pass
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
            except Exception:  # noqa: BLE001 - best-effort; link may be gone
                pass
            await self._client.disconnect()
            self._client = await self._connector(self._ble_device)
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        self._notifying = True

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        _LOGGER.debug("disconnecting (notifying=%s)", self._notifying)
        # Release BlueZ's per-characteristic notify subscription before dropping
        # the link. Skipping this leaves it "acquired", so the next connect's
        # start_notify fails with org.bluez.Error.NotPermitted: Notify acquired,
        # piling up HCI resources until ENOMEM. The link may already be gone, so
        # failing to stop notify must not prevent the disconnect.
        if self._notifying:
            try:
                await client.stop_notify(CHARACTERISTIC_UUID)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            self._notifying = False
        await client.disconnect()

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def _rearm_notify(self) -> None:
        # The Dohm goes notify-deaf on a long-lived link until its CCCD is
        # re-enabled: the device stops emitting notifications even though writes
        # are still ATT-acked, so command replies silently go missing. The
        # official app rewrites 01 00 to the CCCD before every command; mirror
        # that. Best-effort -- some backends (e.g. CoreBluetooth) hide the CCCD,
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
        async with self._lock:
            await self._rearm_notify()
            while not self._queue.empty():
                self._queue.get_nowait()
            started = time.monotonic()
            try:
                await self._client.write_gatt_char(
                    CHARACTERISTIC_UUID, payload, response=True
                )
                acked = time.monotonic()
                raw = await asyncio.wait_for(self._queue.get(), COMMAND_TIMEOUT)
                # The ack/reply split is the diagnostic that matters: a device
                # that acks fast and then never answers has gone notify-deaf,
                # while a slow ack means the link itself is struggling.
                _LOGGER.debug(
                    "%r -> %r (ack %.2fs, reply %.2fs)",
                    payload,
                    bytes(raw),
                    acked - started,
                    time.monotonic() - acked,
                )
            except Exception as err:
                # The link is unusable but not necessarily *down*: a device that
                # has gone notify-deaf still acks writes, and a half-open link
                # still reports is_connected. Either way nothing here reconnects
                # on its own, so without dropping it the coordinator retries the
                # same dead client every poll, forever. Rebuild from scratch
                # instead -- what the official app does when it resumes slowly.
                # Cancellation is not evidence of a bad link, so it is excluded.
                _LOGGER.debug(
                    "%r failed after %.2fs (%s: %s); dropping the link so the "
                    "next command reconnects",
                    payload,
                    time.monotonic() - started,
                    type(err).__name__,
                    err,
                )
                try:
                    await self.disconnect()
                except Exception:  # noqa: BLE001 - best-effort; keep the cause
                    pass
                raise
        message = protocol.parse(raw)
        if isinstance(message, protocol.Failure):
            raise DohmCommandError(f"device rejected {payload!r}: {message.code}")
        return message

    def _require_id(self) -> str:
        if self._device_id is None:
            raise DohmError("device id unknown; call connect() first")
        return self._device_id

    async def identify(self) -> str:
        self._device_id = (await self._command(protocol.query_id())).value
        return self._device_id

    async def set_speed(self, speed: int) -> None:
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(
                f"speed must be {MIN_SPEED}..{MAX_SPEED}, got {speed}"
            )
        await self._command(protocol.set_speed(self._require_id(), speed))

    async def get_speed(self) -> int:
        return (await self._command(protocol.query_speed(self._require_id()))).speed

    async def set_power(self, on: bool) -> None:
        await self._command(protocol.set_power(self._require_id(), on))

    async def get_power(self) -> bool:
        return (await self._command(protocol.query_power(self._require_id()))).on

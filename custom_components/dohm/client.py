"""Async BLE client for the Marpac Dohm.

Wraps the pure protocol layer with a connection and a request/response loop.
Connections go through ``bleak-retry-connector``'s ``establish_connection``,
which works both standalone and inside Home Assistant's Bluetooth stack
(including ESPHome proxies). A ``connector`` can be injected for testing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import protocol
from .const import CHARACTERISTIC_UUID, MAX_SPEED, MIN_SPEED

COMMAND_TIMEOUT = 5.0


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
        self._client = await self._connector(self._ble_device)
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        await self.identify()

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def _command(self, payload: bytes):
        async with self._lock:
            while not self._queue.empty():
                self._queue.get_nowait()
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID, payload, response=True
            )
            raw = await asyncio.wait_for(self._queue.get(), COMMAND_TIMEOUT)
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

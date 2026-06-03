"""DataUpdateCoordinator for the Marpac Dohm.

The device has no useful advertisements for state — it only talks over an active
connection — so we poll power and speed on an interval and keep a persistent
connection (reconnecting through bleak-retry-connector when needed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from bleak_retry_connector import BLEAK_EXCEPTIONS
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DohmClient, DohmError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(seconds=30)


@dataclass
class DohmState:
    """Snapshot of the device's controllable state."""

    power: bool
    speed: int


class DohmCoordinator(DataUpdateCoordinator[DohmState]):
    """Polls and controls a single Dohm over BLE."""

    def __init__(self, hass: HomeAssistant, client: DohmClient, address: str) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self.client = client
        self.address = address

    async def _ensure_connected(self) -> None:
        if self.client.is_connected:
            return
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"{self.address} is not in range")
        await self.client.connect(ble_device)

    async def _async_update_data(self) -> DohmState:
        try:
            await self._ensure_connected()
            return DohmState(
                power=await self.client.get_power(),
                speed=await self.client.get_speed(),
            )
        except DohmError as err:
            raise UpdateFailed(str(err)) from err
        except (*BLEAK_EXCEPTIONS, TimeoutError) as err:
            raise UpdateFailed(f"error talking to Dohm: {err}") from err

    async def async_set_power(self, on: bool) -> None:
        await self._ensure_connected()
        await self.client.set_power(on)
        await self.async_request_refresh()

    async def async_set_speed(self, speed: int) -> None:
        await self._ensure_connected()
        await self.client.set_speed(speed)
        await self.async_request_refresh()

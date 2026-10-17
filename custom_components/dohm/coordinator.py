"""DataUpdateCoordinator for the Marpac Dohm.

The device has no useful advertisements for state — it only talks over an active
connection — so we poll power and speed on an interval and keep a persistent
connection (reconnecting through bleak-retry-connector when needed).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial

from bleak_retry_connector import BLEAK_EXCEPTIONS
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_ble_device_from_address,
    async_process_advertisements,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DohmClient, DohmError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(seconds=30)
READBACK_DELAY = 1.0

# How long to wait for the Dohm to advertise before calling it out of range.
# The cache lookup is a single point in time, so on its own it fails a poll that
# lands in a gap between advertisements -- the phone app looks for the device
# for a moment before connecting, and this is that moment. Kept well inside the
# 30s poll interval so a genuinely dormant device is still reported promptly.
ADVERTISEMENT_WAIT = 10


@dataclass
class DohmState:
    """Snapshot of the device's controllable state."""

    power: bool
    speed: int


class DohmCoordinator(DataUpdateCoordinator[DohmState]):
    """Polls and controls a single Dohm over BLE."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: DohmClient,
        address: str,
    ) -> None:
        # config_entry is passed explicitly rather than left to the ContextVar
        # fallback, which Home Assistant removes in 2026.8.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.address = address
        # Keep a two-read poll from straddling a control command and publishing
        # its old power/speed after the command's acknowledged state.
        self._operation_lock = asyncio.Lock()
        self._control_revision = 0
        self._pending_controls = 0
        self._cancel_readback: Callable[[], None] | None = None
        self.loaded_platform: Platform = Platform.MEDIA_PLAYER

    def _fresh_ble_device(self):
        """The freshest BLEDevice Home Assistant has, or None."""
        return async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_wait_for_device(self):
        """Return a BLEDevice, waiting briefly for one to advertise.

        The cache read is a single point in time. The Dohm advertises in bursts
        rather than continuously, so a poll landing between bursts sees nothing
        even though the device is right there -- hence the short scan window
        before giving up, which is what the phone app does.
        """
        ble_device = self._fresh_ble_device()
        if ble_device is not None:
            return ble_device
        try:
            await async_process_advertisements(
                self.hass,
                lambda service_info: True,
                BluetoothCallbackMatcher(address=self.address, connectable=True),
                BluetoothScanningMode.ACTIVE,
                ADVERTISEMENT_WAIT,
            )
        except TimeoutError:
            return None
        # Take the BLEDevice from the manager rather than the advertisement, so
        # we connect through whichever scanner (proxy or local) actually has it.
        return self._fresh_ble_device()

    async def _ensure_connected(self) -> None:
        if self.client.is_connected:
            return
        ble_device = await self._async_wait_for_device()
        if ble_device is None:
            # Distinguishes "the device stopped advertising" (it dorms after ~1h
            # with no connection) from "we could reach it but the link failed".
            raise UpdateFailed(
                f"{self.address} did not advertise within {ADVERTISEMENT_WAIT}s"
            )
        _LOGGER.debug(
            "reconnecting to %s via %s (rssi %s)",
            self.address,
            getattr(ble_device, "name", "?"),
            getattr(ble_device, "rssi", "?"),
        )
        await self.client.connect(ble_device)

    async def _async_update_data(self) -> DohmState:
        """Poll the device. A failure here is just a failed poll.

        There is deliberately no escalation. Counting failures and clearing the
        bond automatically could not tell a slow link from a stale one, and got
        it wrong in the direction that costs the most: unpairing a Dohm that was
        merely being slow leaves it reachable only after a top-button press.
        Home Assistant already retries on the next interval, forever, which is
        the right response to a link that is simply not ready yet. Clearing a
        genuinely stale bond is a deliberate action in the options flow.
        """
        try:
            async with self._operation_lock:
                # An ordinary poll must respect the same settling window as
                # command readback, including controls queued behind this poll.
                if self._pending_controls or self._cancel_readback is not None:
                    return self.data
                revision = self._control_revision
                await self._ensure_connected()
                state = DohmState(
                    power=await self.client.get_power(),
                    speed=await self.client.get_speed(),
                )
                if revision != self._control_revision:
                    return self.data
                return state
        except DohmError as err:
            raise UpdateFailed(str(err)) from err
        except (*BLEAK_EXCEPTIONS, TimeoutError) as err:
            raise UpdateFailed(f"error talking to Dohm: {err}") from err

    async def async_rebond(self) -> str:
        """Clear the stored bond and pair again; returns what happened.

        Only ever reached because someone asked for it. The link is dropped
        first: unpair/pair run on their own short-lived connection, and the
        next poll rebuilds the normal one.
        """
        # rebond() waits for in-flight commands under the client's lock before
        # disconnecting. Do not tear their transport down outside that lock.
        outcome = await self.client.rebond(self._fresh_ble_device())
        _LOGGER.warning("re-bonded %s on request: %s", self.address, outcome)
        await self.async_request_refresh()
        return outcome

    @asynccontextmanager
    async def _control_update(self) -> AsyncIterator[None]:
        # Invalidate reads when the request arrives, not only after the write
        # gets the lock. Never cancel a BLE exchange just to debounce readback.
        self._control_revision += 1
        self._pending_controls += 1
        self._async_cancel_readback()
        try:
            async with self._operation_lock:
                await self._ensure_connected()
                yield
        finally:
            self._pending_controls -= 1
            if not self._pending_controls and not self._shutdown_requested:
                self._cancel_readback = async_call_later(
                    self.hass,
                    READBACK_DELAY,
                    partial(self._async_confirm_controls, self._control_revision),
                )

    def _async_cancel_readback(self) -> None:
        if self._cancel_readback is not None:
            self._cancel_readback()
            self._cancel_readback = None

    async def _async_confirm_controls(self, revision: int, _now) -> None:
        if (
            self._shutdown_requested
            or self._pending_controls
            or revision != self._control_revision
        ):
            return
        self._cancel_readback = None
        # Use a real refresh: async_request_refresh has a separate HA cooldown
        # and would delay confirmation beyond the trailing one-second timer.
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        self._async_cancel_readback()
        await super().async_shutdown()

    async def async_set_power(self, on: bool) -> None:
        async with self._control_update():
            await self.client.set_power(on)
            self._async_snap(power=on)

    async def async_set_speed(self, speed: int) -> None:
        async with self._control_update():
            await self.client.set_speed(speed)
            self._async_snap(speed=speed)

    def _async_snap(
        self, *, power: bool | None = None, speed: int | None = None
    ) -> None:
        """Publish the value the device actually took, without waiting for a poll.

        The Dohm has ten levels, so a slider dropped at 43% becomes level 5 --
        50%. Leaving the control at 43% until the confirming poll comes back
        reads as if the device accepted 43%, which it never can. Publishing the
        accepted value here makes the control snap to what the Dohm is really
        doing, and says so at the moment of the change rather than a BLE
        round-trip later.

        Optimistic only as far as the ack: the command was already ack'd by the
        time we get here. Confirmation is debounced until one second after
        the last control completes, so rapid taps do not read unsettled state.
        The normal polling interval still handles external changes.
        """
        if self.data is None:
            return
        self.async_set_updated_data(
            DohmState(
                power=self.data.power if power is None else power,
                speed=self.data.speed if speed is None else speed,
            )
        )

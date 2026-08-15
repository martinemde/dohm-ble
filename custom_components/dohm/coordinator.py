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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DohmClient, DohmError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(seconds=30)

# Consecutive failed polls before we stop assuming a transient dropout and treat
# the link as bonded-but-dead. At a 30s interval that is 90s -- two chances for a
# blip to clear on its own -- and the escalation runs once per outage, not per
# poll, so the cost of guessing wrong is one wasted reconnect.
REBOND_AFTER_FAILURES = 3


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
        self._failures = 0
        self._rebonded = False

    async def _ensure_connected(self) -> None:
        if self.client.is_connected:
            return
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            # Distinguishes "the device stopped advertising" (it dorms after ~1h
            # with no connection) from "we could reach it but the link failed".
            raise UpdateFailed(f"{self.address} is not in range")
        _LOGGER.debug(
            "reconnecting to %s via %s (rssi %s)",
            self.address,
            getattr(ble_device, "name", "?"),
            getattr(ble_device, "rssi", "?"),
        )
        await self.client.connect(ble_device)

    async def _async_update_data(self) -> DohmState:
        # "Not in range" leaves _ensure_connected as UpdateFailed and is
        # deliberately not counted: the device dorms after ~1h idle, which is
        # normal and says nothing about the bond. Only failures against a device
        # we could actually reach escalate.
        try:
            await self._ensure_connected()
            state = DohmState(
                power=await self.client.get_power(),
                speed=await self.client.get_speed(),
            )
        except DohmError as err:
            await self._async_note_failure()
            raise UpdateFailed(str(err)) from err
        except (*BLEAK_EXCEPTIONS, TimeoutError) as err:
            await self._async_note_failure()
            raise UpdateFailed(f"error talking to Dohm: {err}") from err
        self._note_success()
        return state

    async def _async_note_failure(self) -> None:
        """Count a failed poll and, past the threshold, try to re-bond once."""
        self._failures += 1
        if self._failures < REBOND_AFTER_FAILURES or self._rebonded:
            return
        # Once per outage. If clearing the bond didn't fix it, repeating it every
        # 30s only churns the proxy's NVS and keeps the device connected to
        # nothing; the repair issue below is then the actionable part.
        self._rebonded = True
        try:
            outcome = await self.client.rebond(
                async_ble_device_from_address(self.hass, self.address, connectable=True)
            )
        except Exception as err:  # noqa: BLE001 - diagnostics; poll still fails
            _LOGGER.warning(
                "re-bonding %s after %d failed polls did not complete (%s: %s)",
                self.address,
                self._failures,
                type(err).__name__,
                err,
            )
        else:
            _LOGGER.warning(
                "re-bonded %s after %d failed polls: %s",
                self.address,
                self._failures,
                outcome,
            )
        # Raised either way: re-bonding can only clear *our* stale key, and the
        # device grants a new bond only in pairing mode. If the next poll
        # succeeds this is deleted again, so a self-healed outage stays silent.
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"stale_bond_{self.address}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="stale_bond",
            translation_placeholders={"address": self.address},
        )

    def _note_success(self) -> None:
        if not self._failures:
            return
        self._failures = 0
        self._rebonded = False
        ir.async_delete_issue(self.hass, DOMAIN, f"stale_bond_{self.address}")

    async def async_set_power(self, on: bool) -> None:
        await self._ensure_connected()
        await self.client.set_power(on)
        await self.async_request_refresh()

    async def async_set_speed(self, speed: int) -> None:
        await self._ensure_connected()
        await self.client.set_speed(speed)
        await self.async_request_refresh()

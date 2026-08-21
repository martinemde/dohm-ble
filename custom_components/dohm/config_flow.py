"""Config flow for the Marpac Dohm integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from . import protocol
from .client import DohmClient, DohmError, DohmRebondUnsupported
from .const import COMMAND_SERVICE_UUID, DOMAIN, LOCAL_NAME_PREFIX

_LOGGER = logging.getLogger(__name__)


def _is_dohm(info: BluetoothServiceInfoBleak) -> bool:
    """Identify a Dohm. Its advertisement carries the command service UUID but
    no local name, so match on the service UUID (with a name fallback)."""
    if COMMAND_SERVICE_UUID in info.service_uuids:
        return True
    return bool(info.name) and info.name.upper().startswith(LOCAL_NAME_PREFIX)


def _display_name(info: BluetoothServiceInfoBleak) -> str:
    if info.name and info.name.upper().startswith(LOCAL_NAME_PREFIX):
        return info.name
    return f"Yogasleep Dohm Connect ({info.address})"


class DohmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover Dohm devices over Bluetooth and create entries."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DohmOptionsFlow()

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}  # address -> name

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via the Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _display_name(discovery_info)}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a single discovered device."""
        assert self._discovery is not None
        name = _display_name(self._discovery)
        if user_input is not None:
            return self._create_entry(self._discovery.address, name)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick from discovered devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create_entry(address, self._discovered[address])

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not _is_dohm(info):
                continue
            self._discovered[info.address] = _display_name(info)

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )

    def _create_entry(self, address: str, name: str) -> ConfigFlowResult:
        return self.async_create_entry(title=name, data={CONF_ADDRESS: address})


class DohmOptionsFlow(OptionsFlow):
    """Deliberate recovery actions for a Dohm that has stopped answering.

    Re-bonding lives here rather than on a failure counter. The signature it
    fixes -- a link that connects and ATT-acks writes but never replies -- is
    indistinguishable from a link that is merely slow, and guessing wrong
    unpairs a working Dohm, which then needs a top-button press to come back.
    A person looking at an unavailable entity can tell the difference; a
    counter cannot.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["rebond"])

    async def async_step_rebond(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Clear the pairing key this side holds and ask for a new one."""
        if user_input is None:
            return self.async_show_form(step_id="rebond")
        try:
            outcome = await self._async_rebond()
        except DohmRebondUnsupported:
            return self.async_abort(reason="rebond_unsupported")
        except Exception:  # surfaced to the person who asked, not swallowed
            _LOGGER.exception("re-bonding %s failed", self.config_entry.title)
            return self.async_abort(reason="rebond_failed")
        return self.async_abort(
            reason="rebond_done", description_placeholders={"outcome": outcome}
        )

    async def _async_rebond(self) -> str:
        """Re-bond whether or not the entry is up.

        A stale bond makes the first poll fail, which fails setup -- so the
        entry that most needs this is usually the one with no coordinator to
        ask. Fall back to a throwaway client in that case, then let Home
        Assistant retry the setup that was failing.
        """
        if self.config_entry.state is ConfigEntryState.LOADED:
            return await self.config_entry.runtime_data.async_rebond()

        address = self.config_entry.data[CONF_ADDRESS]
        ble_device = async_ble_device_from_address(self.hass, address, connectable=True)
        if ble_device is None:
            raise DohmError(f"{address} is not advertising")
        client = DohmClient(
            ble_device, device_id=protocol.device_id_from_address(address)
        )
        outcome = await client.rebond()
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return outcome

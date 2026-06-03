"""Config flow for the Marpac Dohm integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import COMMAND_SERVICE_UUID, DOMAIN, LOCAL_NAME_PREFIX


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

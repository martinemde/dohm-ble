"""The Marpac Dohm integration.

Home Assistant imports are deferred into the function bodies (and TYPE_CHECKING)
on purpose: it keeps the vendored protocol/client engine importable and testable
without a full Home Assistant install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import DohmCoordinator

    type DohmConfigEntry = ConfigEntry[DohmCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DohmConfigEntry) -> bool:
    """Set up Marpac Dohm from a config entry."""
    from homeassistant.components.bluetooth import async_ble_device_from_address
    from homeassistant.const import CONF_ADDRESS, Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from . import protocol
    from .client import DohmClient
    from .coordinator import DohmCoordinator

    address: str = entry.data[CONF_ADDRESS]
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"Could not find Dohm with address {address}")

    client = DohmClient(
        ble_device,
        # The id is the lower three bytes of the address, so there is nothing to
        # look up, store, or keep in sync -- and no i$ on the connect path.
        device_id=protocol.device_id_from_address(address),
        ble_device_callback=lambda: async_ble_device_from_address(
            hass, address, connectable=True
        ),
    )
    coordinator = DohmCoordinator(hass, entry, client, address)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.MEDIA_PLAYER])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DohmConfigEntry) -> bool:
    """Unload a config entry."""
    from homeassistant.const import Platform

    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform.MEDIA_PLAYER]
    )
    if unloaded:
        await entry.runtime_data.client.disconnect()
    return unloaded

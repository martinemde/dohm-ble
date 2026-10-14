"""Optional ten-step fan platform for the Dohm sound machine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MAX_SPEED, MODEL, MODEL_ID
from .coordinator import DohmCoordinator
from .media_player import _area_name
from .volume import speed_to_volume, volume_to_speed

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selected fan entity using the same coordinator as media players."""
    async_add_entities([DohmFan(entry.runtime_data, entry, _area_name(hass, entry))])


class DohmFan(CoordinatorEntity[DohmCoordinator], FanEntity):
    """A Dohm exposed as on/off and ten discrete fan speeds."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_speed_count = MAX_SPEED
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(
        self, coordinator: DohmCoordinator, entry: ConfigEntry, area: str | None
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, entry.data[CONF_ADDRESS])},
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            model_id=MODEL_ID,
            name=entry.title,
            suggested_area=area,
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.power

    @property
    def percentage(self) -> int:
        """Report zero while off, otherwise the actual ten-step speed."""
        if not self.is_on:
            return 0
        return round(speed_to_volume(self.coordinator.data.speed) * 100)

    async def async_set_percentage(self, percentage: int) -> None:
        """Zero powers off; a positive setting snaps up and powers on."""
        if percentage <= 0:
            await self.async_turn_off()
            return
        speed = volume_to_speed(percentage / 100)
        if not self.is_on:
            await self.coordinator.async_set_power(True)
        await self.coordinator.async_set_speed(speed)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Resume the stored speed unless an explicit percentage is given."""
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)

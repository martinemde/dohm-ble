"""Fan platform for the Marpac Dohm."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)
from homeassistant.util.scaling import int_states_in_range

from .const import DOMAIN, MANUFACTURER, MAX_SPEED, MIN_SPEED, MODEL, MODEL_ID
from .coordinator import DohmCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

SPEED_RANGE = (MIN_SPEED, MAX_SPEED)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Dohm fan from a config entry."""
    async_add_entities([DohmFan(entry.runtime_data, entry)])


class DohmFan(CoordinatorEntity[DohmCoordinator], FanEntity):
    """A Marpac Dohm exposed as a fan (on/off + speed 1..10)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: DohmCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            model_id=MODEL_ID,
            name=entry.title,
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.power

    @property
    def percentage(self) -> int:
        if not self.coordinator.data.power:
            return 0
        return ranged_value_to_percentage(SPEED_RANGE, self.coordinator.data.speed)

    @property
    def speed_count(self) -> int:
        return int_states_in_range(SPEED_RANGE)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.coordinator.async_set_power(False)
            return
        speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        if not self.coordinator.data.power:
            await self.coordinator.async_set_power(True)
        await self.coordinator.async_set_speed(speed)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)

"""Media player platform for the Marpac Dohm.

The Dohm is a sound machine, so what the user actually adjusts is loudness.
media_player is the only Home Assistant domain that carries volume as
first-class state, which also means the HassSetVolume voice intent works.

It plays no media: no source, no metadata, no transport controls. Only power
and volume are declared, so the entity is never offered as a playback target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MAX_SPEED, MIN_SPEED, MODEL, MODEL_ID
from .coordinator import DohmCoordinator
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
    """Set up the Dohm media player from a config entry."""
    async_add_entities([DohmMediaPlayer(entry.runtime_data, entry)])


class DohmMediaPlayer(CoordinatorEntity[DohmCoordinator], MediaPlayerEntity):
    """A Marpac Dohm exposed as a speaker (on/off + volume)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
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
    def state(self) -> MediaPlayerState:
        if not self.coordinator.data.power:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING

    @property
    def volume_level(self) -> float:
        return speed_to_volume(self.coordinator.data.speed)

    async def async_set_volume_level(self, volume: float) -> None:
        speed = volume_to_speed(volume)
        # Adjusting the volume on a device that is off is a request to hear it.
        if not self.coordinator.data.power:
            await self.coordinator.async_set_power(True)
        await self.coordinator.async_set_speed(speed)

    async def async_volume_up(self) -> None:
        # Stepped on the device's own scale rather than the default 0.1 nudge,
        # so every press moves exactly one of the ten settings.
        await self._async_step_speed(1)

    async def async_volume_down(self) -> None:
        await self._async_step_speed(-1)

    async def _async_step_speed(self, delta: int) -> None:
        speed = self.coordinator.data.speed + delta
        if not MIN_SPEED <= speed <= MAX_SPEED:
            return
        await self.coordinator.async_set_speed(speed)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_power(False)

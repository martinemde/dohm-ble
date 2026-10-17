"""Home Assistant media-player tests (skipped in the BLE-only environment)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.media_player import MediaPlayerState

from custom_components.dohm.coordinator import DohmState
from custom_components.dohm.media_player import DohmMediaPlayer


@pytest.fixture
def calls():
    return []


@pytest.fixture
def media(calls):
    coordinator = SimpleNamespace(
        data=DohmState(power=True, speed=5),
        last_update_success=True,
        async_set_speed=AsyncMock(),
        async_set_power=AsyncMock(),
    )

    async def set_power(on):
        calls.append(("power", on))
        coordinator.data.power = on

    async def set_speed(speed):
        calls.append(("speed", speed))
        coordinator.data.speed = speed

    coordinator.async_set_power.side_effect = set_power
    coordinator.async_set_speed.side_effect = set_speed
    entry = SimpleNamespace(
        data={"address": "AA:BB:CC:01:36:C4"}, unique_id="stable", title="Dohm"
    )
    return DohmMediaPlayer(coordinator, entry, None)


@pytest.mark.parametrize("volume", [0, -0.01])
@pytest.mark.parametrize("power", [False, True])
async def test_media_zero_volume_turns_off_and_preserves_speed(media, volume, power):
    media.coordinator.data.power = power
    await media.async_set_volume_level(volume)
    media.coordinator.async_set_power.assert_awaited_once_with(False)
    media.coordinator.async_set_speed.assert_not_awaited()
    assert media.state == MediaPlayerState.OFF
    assert media.volume_level == 0
    assert media.coordinator.data.speed == 5
    assert media.unique_id == "stable"


@pytest.mark.parametrize("speed", range(1, 11))
def test_media_reports_zero_only_while_off(media, speed):
    media.coordinator.data.speed = speed
    assert media.volume_level == speed / 10
    media.coordinator.data.power = False
    assert media.volume_level == 0
    assert media.coordinator.data.speed == speed


@pytest.mark.parametrize("volume,speed", [(0.01, 1), (0.44, 4), (0.45, 5), (1, 10)])
@pytest.mark.parametrize("power", [False, True])
async def test_media_positive_volume_rounds_to_nearest_step_and_powers_on(
    media, calls, volume, speed, power
):
    media.coordinator.data.power = power
    await media.async_set_volume_level(volume)
    assert calls == ([] if power else [("power", True)]) + [("speed", speed)]
    assert media.state == MediaPlayerState.PLAYING
    assert media.volume_level == speed / 10


@pytest.mark.parametrize("turn_off", ["zero_volume", "power_control"])
@pytest.mark.parametrize("speed", range(1, 11))
async def test_media_turn_on_resumes_last_speed_after_turning_off(
    media, turn_off, speed
):
    media.coordinator.data.speed = speed
    assert media.volume_level == speed / 10
    if turn_off == "zero_volume":
        await media.async_set_volume_level(0)
    else:
        await media.async_turn_off()
    assert media.state == MediaPlayerState.OFF
    assert media.volume_level == 0
    assert media.coordinator.data.speed == speed
    media.coordinator.async_set_power.reset_mock()
    await media.async_turn_on()
    media.coordinator.async_set_power.assert_awaited_once_with(True)
    media.coordinator.async_set_speed.assert_not_awaited()
    assert media.state == MediaPlayerState.PLAYING
    assert media.volume_level == speed / 10
    assert media.coordinator.data.speed == speed


async def test_media_zero_volume_failure_does_not_send_speed(media):
    media.coordinator.async_set_power.side_effect = RuntimeError("BLE command failed")
    with pytest.raises(RuntimeError, match="BLE command failed"):
        await media.async_set_volume_level(0)
    media.coordinator.async_set_speed.assert_not_awaited()


@pytest.mark.parametrize("speed", [0, None, -1, 11])
async def test_media_turn_on_uses_ten_percent_when_previous_speed_unknown(
    media, calls, speed
):
    media.coordinator.data.power = False
    media.coordinator.data.speed = speed
    await media.async_turn_on()
    assert calls == [("speed", 1), ("power", True)]
    assert media.state == MediaPlayerState.PLAYING
    assert media.volume_level == 0.1


async def test_media_turn_on_without_snapshot_seeds_speed_before_power(media, calls):
    media.coordinator.data = None

    async def set_speed(value):
        calls.append(("speed", value))
        media.coordinator.data = DohmState(power=False, speed=value)

    media.coordinator.async_set_speed.side_effect = set_speed
    await media.async_turn_on()
    assert calls == [("speed", 1), ("power", True)]
    assert media.volume_level == 0.1


async def test_media_unknown_speed_failure_does_not_turn_on(media):
    media.coordinator.data.speed = 0
    media.coordinator.data.power = False
    media.coordinator.async_set_speed.side_effect = RuntimeError("BLE command failed")
    with pytest.raises(RuntimeError, match="BLE command failed"):
        await media.async_turn_on()
    media.coordinator.async_set_power.assert_not_awaited()

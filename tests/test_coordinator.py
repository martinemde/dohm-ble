"""Coordinator state updates through real Home Assistant entities."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant

from custom_components.dohm.coordinator import DohmCoordinator, DohmState
from custom_components.dohm.media_player import DohmMediaPlayer


@pytest.fixture
async def setup(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    client = SimpleNamespace(
        is_connected=True,
        get_power=AsyncMock(return_value=False),
        get_speed=AsyncMock(return_value=5),
        set_power=AsyncMock(),
        set_speed=AsyncMock(),
    )
    coordinator = DohmCoordinator(hass, None, client, "AA:BB:CC:01:36:C4")
    coordinator.data = DohmState(power=False, speed=5)
    entry = SimpleNamespace(
        data={"address": coordinator.address}, unique_id="stable", title="Dohm"
    )
    media = DohmMediaPlayer(coordinator, entry, None)
    yield coordinator, client, media
    await coordinator.async_shutdown()


@pytest.mark.parametrize("initial_power", [False, True])
async def test_media_power_ack_is_not_immediately_overwritten_by_readback(
    setup, initial_power
):
    coordinator, client, media = setup
    coordinator.data.power = initial_power
    client.get_power.return_value = initial_power

    # Model firmware still reporting its old state immediately after an ACK.
    async def refresh():
        coordinator.async_set_updated_data(await coordinator._async_update_data())

    coordinator.async_request_refresh = AsyncMock(side_effect=refresh)
    action = media.async_turn_off if initial_power else media.async_turn_on
    await action()
    assert coordinator.data.power is not initial_power
    assert media.state == (
        MediaPlayerState.OFF if initial_power else MediaPlayerState.PLAYING
    )
    assert media.volume_level == (0 if initial_power else 0.5)
    client.set_power.assert_awaited_once_with(not initial_power)
    client.get_power.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


async def test_poll_cannot_publish_old_power_after_a_media_command(setup):
    coordinator, client, media = setup
    reading_speed = asyncio.Event()
    finish_poll = asyncio.Event()
    command_started = asyncio.Event()
    published_power = []
    coordinator.async_update_listeners = Mock(
        side_effect=lambda: published_power.append(coordinator.data.power)
    )
    coordinator.async_request_refresh = AsyncMock()

    async def speed():
        reading_speed.set()
        await finish_poll.wait()
        return 5

    client.get_speed.side_effect = speed

    async def turn_on():
        command_started.set()
        await media.async_turn_on()

    # A poll has read OFF and is still fetching speed when ON is requested.
    poll = asyncio.create_task(coordinator.async_refresh())
    await reading_speed.wait()
    command = asyncio.create_task(turn_on())
    await command_started.wait()
    try:
        client.set_power.assert_not_awaited()
    finally:
        finish_poll.set()
        await asyncio.gather(poll, command)

    assert media.state == MediaPlayerState.PLAYING
    assert published_power[-1] is True
    client.set_power.assert_awaited_once_with(True)


async def test_speed_ack_waits_for_normal_poll_and_external_changes_are_read(setup):
    coordinator, client, _ = setup
    coordinator.async_request_refresh = AsyncMock()
    await coordinator.async_set_speed(7)
    assert coordinator.data == DohmState(power=False, speed=7)
    coordinator.async_request_refresh.assert_not_awaited()

    # The next ordinary poll is authoritative, including physical controls.
    client.get_power.return_value = True
    client.get_speed.return_value = 3
    await coordinator.async_refresh()
    assert coordinator.data == DohmState(power=True, speed=3)


async def test_failed_power_command_does_not_publish_requested_state(setup):
    coordinator, client, media = setup
    client.set_power.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError, match="write failed"):
        await media.async_turn_on()
    assert media.state == MediaPlayerState.OFF
    assert coordinator.data.speed == 5

    client.set_power.side_effect = None
    await media.async_turn_on()
    assert media.state == MediaPlayerState.PLAYING

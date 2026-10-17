"""Coordinator state updates through real Home Assistant entities."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant

from custom_components.dohm import coordinator as coordinator_module
from custom_components.dohm.coordinator import (
    READBACK_DELAY,
    DohmCoordinator,
    DohmState,
)
from custom_components.dohm.media_player import DohmMediaPlayer


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    timers = []

    def schedule(hass, delay, action):
        timer = SimpleNamespace(delay=delay, action=action, cancelled=False)
        timers.append(timer)

        def cancel():
            timer.cancelled = True

        return cancel

    monkeypatch.setattr(coordinator_module, "async_call_later", schedule)
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
    coordinator.test_timers = timers
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
    await coordinator.test_timers[-1].action(None)
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


async def test_rapid_power_and_volume_debounce_only_readback(setup):
    coordinator, client, media = setup
    await media.async_turn_on()
    await media.async_volume_up()
    await media.async_volume_up()
    await media.async_volume_down()
    await media.async_turn_off()
    await media.async_turn_on()

    assert [call.args for call in client.set_power.await_args_list] == [
        (True,),
        (False,),
        (True,),
    ]
    assert [call.args for call in client.set_speed.await_args_list] == [
        (6,),
        (7,),
        (6,),
    ]
    assert media.state == MediaPlayerState.PLAYING
    assert media.volume_level == 0.6
    client.get_power.assert_not_awaited()
    timers = coordinator.test_timers
    assert len(timers) == 6
    assert all(timer.cancelled for timer in timers[:-1])
    assert not timers[-1].cancelled
    assert timers[-1].delay == READBACK_DELAY == 1.0

    # Even a regular refresh cannot read during the settling window.
    await coordinator.async_refresh()
    client.get_power.assert_not_awaited()
    client.get_power.return_value = True
    client.get_speed.return_value = 6
    await timers[-1].action(None)
    client.get_power.assert_awaited_once()
    client.get_speed.assert_awaited_once()
    assert coordinator.data == DohmState(power=True, speed=6)

    # After confirmation, normal polls still reconcile physical controls.
    client.get_power.return_value = False
    await coordinator.async_refresh()
    assert media.state == MediaPlayerState.OFF


async def test_poll_started_before_new_command_does_not_publish_old_snapshot(setup):
    coordinator, client, media = setup
    # HA holds the acknowledged ON, but an older read still reports OFF.
    coordinator.data.power = True
    reading_speed = asyncio.Event()
    finish_poll = asyncio.Event()
    command_started = asyncio.Event()
    published = []
    coordinator.async_update_listeners = Mock(
        side_effect=lambda: published.append(coordinator.data.power)
    )

    async def read_speed():
        reading_speed.set()
        await finish_poll.wait()
        return 5

    client.get_speed.side_effect = read_speed
    poll = asyncio.create_task(coordinator.async_refresh())
    await reading_speed.wait()

    async def command():
        command_started.set()
        await media.async_volume_up()

    control = asyncio.create_task(command())
    await command_started.wait()
    finish_poll.set()
    await asyncio.gather(poll, control)
    assert published and all(published)
    assert media.state == MediaPlayerState.PLAYING
    assert media.volume_level == 0.6


async def test_poll_queued_during_slow_write_does_not_read_immediately(setup):
    coordinator, client, media = setup
    writing = asyncio.Event()
    finish_write = asyncio.Event()
    poll_started = asyncio.Event()

    async def write(on):
        writing.set()
        await finish_write.wait()

    client.set_power.side_effect = write
    control = asyncio.create_task(media.async_turn_on())
    await writing.wait()

    async def poll():
        poll_started.set()
        await coordinator.async_refresh()

    polling = asyncio.create_task(poll())
    await poll_started.wait()
    finish_write.set()
    await asyncio.gather(control, polling)
    assert media.state == MediaPlayerState.PLAYING
    client.get_power.assert_not_awaited()
    client.get_speed.assert_not_awaited()


async def test_readback_reconciles_disagreement_without_resending_command(setup):
    coordinator, client, media = setup
    await media.async_turn_on()
    assert media.state == MediaPlayerState.PLAYING
    # Once idle, readback is authoritative even if it disagrees with the ACK.
    await coordinator.test_timers[-1].action(None)
    assert media.state == MediaPlayerState.OFF
    client.set_power.assert_awaited_once_with(True)


async def test_shutdown_cancels_pending_readback(setup):
    coordinator, client, media = setup
    await media.async_turn_on()
    timer = coordinator.test_timers[-1]
    await coordinator.async_shutdown()
    assert timer.cancelled
    assert coordinator._cancel_readback is None
    await timer.action(None)
    client.get_power.assert_not_awaited()


async def test_obsolete_timer_callback_cannot_steal_new_readback(setup):
    coordinator, client, media = setup
    await media.async_turn_on()
    old_timer = coordinator.test_timers[-1]
    await media.async_volume_up()
    new_timer = coordinator.test_timers[-1]

    # A callback dispatched just before cancellation may already be runnable.
    await old_timer.action(None)
    client.get_power.assert_not_awaited()
    assert coordinator._cancel_readback is not None
    assert not new_timer.cancelled

    client.get_power.return_value = True
    client.get_speed.return_value = 6
    await new_timer.action(None)
    client.get_power.assert_awaited_once()
    assert media.volume_level == 0.6


async def test_queued_controls_get_one_timer_after_last_ack(setup):
    coordinator, client, media = setup
    writing = asyncio.Event()
    finish_write = asyncio.Event()
    second_started = asyncio.Event()

    async def write(on):
        writing.set()
        await finish_write.wait()

    client.set_power.side_effect = write
    first = asyncio.create_task(media.async_turn_on())
    await writing.wait()

    async def second():
        second_started.set()
        await coordinator.async_set_speed(7)

    following = asyncio.create_task(second())
    await second_started.wait()
    assert not coordinator.test_timers
    finish_write.set()
    await asyncio.gather(first, following)
    assert len(coordinator.test_timers) == 1
    assert coordinator.data == DohmState(power=True, speed=7)
    client.set_power.assert_awaited_once_with(True)
    client.set_speed.assert_awaited_once_with(7)
    client.get_power.assert_not_awaited()


async def test_failed_write_still_confirms_actual_state_after_idle(setup):
    coordinator, client, media = setup
    client.set_power.side_effect = RuntimeError("reply lost")
    with pytest.raises(RuntimeError, match="reply lost"):
        await media.async_turn_on()
    assert media.state == MediaPlayerState.OFF
    assert coordinator._pending_controls == 0

    # The device may have applied a write even though its reply was lost.
    client.get_power.return_value = True
    await coordinator.test_timers[-1].action(None)
    assert media.state == MediaPlayerState.PLAYING
    client.set_power.assert_awaited_once_with(True)


async def test_shutdown_during_write_does_not_schedule_readback(setup):
    coordinator, client, media = setup
    writing = asyncio.Event()
    finish_write = asyncio.Event()

    async def write(on):
        writing.set()
        await finish_write.wait()

    client.set_power.side_effect = write
    control = asyncio.create_task(media.async_turn_on())
    await writing.wait()
    await coordinator.async_shutdown()
    finish_write.set()
    await control
    assert not coordinator.test_timers
    assert coordinator._pending_controls == 0

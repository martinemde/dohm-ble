"""Home Assistant fan tests (skipped in the BLE-only environment)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.fan import FanEntityFeature

from custom_components.dohm.coordinator import DohmState
from custom_components.dohm.fan import DohmFan, async_setup_entry


@pytest.fixture
def entity():
    coordinator = SimpleNamespace(
        data=DohmState(power=True, speed=5),
        last_update_success=True,
        async_set_speed=AsyncMock(),
        async_set_power=AsyncMock(),
    )
    entry = SimpleNamespace(
        data={"address": "AA:BB:CC:01:36:C4"}, unique_id="stable", title="Dohm"
    )
    return DohmFan(coordinator, entry, "Bedroom")


@pytest.mark.parametrize("speed", range(1, 11))
def test_fan_reports_ten_steps(entity, speed):
    entity.coordinator.data.speed = speed
    assert entity.percentage == speed * 10
    assert entity.is_on
    assert entity.speed_count == 10
    assert entity.percentage_step == 10
    entity.coordinator.data.power = False
    assert entity.percentage == 0
    assert not entity.is_on
    assert entity.coordinator.data.speed == speed


def test_fan_identity_features_and_availability(entity):
    assert entity.unique_id == "stable"
    assert entity.device_info["identifiers"] == {("dohm", "stable")}
    assert entity.device_info["suggested_area"] == "Bedroom"
    assert entity.supported_features == (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    assert entity.available
    entity.coordinator.last_update_success = False
    assert not entity.available


@pytest.mark.parametrize(
    "percentage,speed",
    [
        (1, 1),
        (10, 1),
        (11, 2),
        (20, 2),
        (21, 3),
        (43, 5),
        (50, 5),
        (51, 6),
        (90, 9),
        (91, 10),
        (100, 10),
        (150, 10),
    ],
)
@pytest.mark.parametrize("power", [False, True])
async def test_percentage_rounds_up_and_powers_on(entity, percentage, speed, power):
    entity.coordinator.data.power = power
    calls = []

    async def set_power(on):
        calls.append(("power", on))
        entity.coordinator.data.power = on

    async def set_speed(value):
        calls.append(("speed", value))
        entity.coordinator.data.speed = value

    entity.coordinator.async_set_power.side_effect = set_power
    entity.coordinator.async_set_speed.side_effect = set_speed
    await entity.async_set_percentage(percentage)
    assert calls == ([] if power else [("power", True)]) + [("speed", speed)]
    assert entity.percentage == speed * 10


@pytest.mark.parametrize("percentage", [0, -1])
async def test_zero_turns_off_without_speed_write(entity, percentage):
    await entity.async_set_percentage(percentage)
    entity.coordinator.async_set_power.assert_awaited_once_with(False)
    entity.coordinator.async_set_speed.assert_not_awaited()


async def test_turn_on_resumes_speed_and_turn_off_keeps_speed(entity):
    entity.coordinator.data.power = False
    await entity.async_turn_on()
    entity.coordinator.async_set_power.assert_awaited_once_with(True)
    entity.coordinator.async_set_speed.assert_not_awaited()
    assert entity.coordinator.data.speed == 5
    entity.coordinator.async_set_power.reset_mock()
    await entity.async_turn_off()
    entity.coordinator.async_set_power.assert_awaited_once_with(False)
    entity.coordinator.async_set_speed.assert_not_awaited()


@pytest.mark.parametrize("percentage", [0, 43, 100])
async def test_turn_on_explicit_percentage(entity, percentage):
    entity.coordinator.data.power = False
    await entity.async_turn_on(percentage=percentage)
    entity.coordinator.async_set_power.assert_awaited_once_with(percentage > 0)
    if percentage:
        entity.coordinator.async_set_speed.assert_awaited_once_with(
            5 if percentage == 43 else 10
        )
    else:
        entity.coordinator.async_set_speed.assert_not_awaited()


async def test_fan_setup_adds_one_entity(monkeypatch, entity):
    entry = SimpleNamespace(
        data={"address": "address"},
        unique_id="stable",
        title="Dohm",
        runtime_data=entity.coordinator,
    )
    monkeypatch.setattr("custom_components.dohm.fan._area_name", lambda *_: "Bedroom")
    add = Mock()
    await async_setup_entry(SimpleNamespace(), entry, add)
    add.assert_called_once()
    entities = add.call_args.args[0]
    assert len(entities) == 1
    assert isinstance(entities[0], DohmFan)
    assert entities[0].coordinator is entry.runtime_data


@pytest.mark.parametrize("percentage", range(1, 101))
async def test_all_integer_percentages_use_correct_step(entity, percentage):
    await entity.async_set_percentage(percentage)
    entity.coordinator.async_set_speed.assert_awaited_once_with((percentage + 9) // 10)


async def test_ble_control_failure_does_not_send_followup_speed(entity):
    entity.coordinator.data.power = False
    entity.coordinator.async_set_power.side_effect = RuntimeError("BLE command failed")
    with pytest.raises(RuntimeError, match="BLE command failed"):
        await entity.async_set_percentage(43)
    entity.coordinator.async_set_speed.assert_not_awaited()

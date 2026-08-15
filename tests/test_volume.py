"""Tests for the volume level <-> device speed mapping."""

import pytest

from custom_components.dohm.const import MAX_SPEED, MIN_SPEED
from custom_components.dohm.volume import speed_to_volume, volume_to_speed


# --- Reporting the device's speed as a volume level --------------------------

@pytest.mark.parametrize(
    ("speed", "volume"),
    [(1, 0.1), (2, 0.2), (5, 0.5), (9, 0.9), (10, 1.0)],
)
def test_speed_maps_onto_the_full_volume_range(speed, volume):
    assert speed_to_volume(speed) == pytest.approx(volume)


def test_slowest_speed_is_not_reported_as_silent():
    # The Dohm has no silent setting, so its quietest speed must still report
    # some volume -- 0.0 would render the slider as "off".
    assert speed_to_volume(MIN_SPEED) > 0


# --- Turning a requested volume into a speed the device accepts --------------

@pytest.mark.parametrize(
    ("volume", "speed"),
    [(0.1, 1), (0.2, 2), (0.5, 5), (0.9, 9), (1.0, 10)],
)
def test_exact_steps_round_trip(volume, speed):
    assert volume_to_speed(volume) == speed
    assert speed_to_volume(volume_to_speed(volume)) == pytest.approx(volume)


@pytest.mark.parametrize(
    ("volume", "speed"),
    [(0.41, 5), (0.45, 5), (0.49, 5), (0.01, 1), (0.99, 10)],
)
def test_volume_inside_a_step_rounds_up_to_that_step(volume, speed):
    # Rounding up means any drag past a notch lands on the next one, matching
    # what the slider shows rather than dropping back down.
    assert volume_to_speed(volume) == speed


def test_zero_volume_clamps_to_the_quietest_speed():
    # The slider reaches 0.0 but the device cannot go silent, and a media
    # player at volume 0 is still playing -- turning it off is a separate call.
    assert volume_to_speed(0.0) == MIN_SPEED


def test_volume_never_exceeds_the_devices_top_speed():
    # 11 is rejected by the device with "Failed 03$", so clamping matters.
    assert volume_to_speed(1.5) == MAX_SPEED


def test_every_speed_is_reachable_from_some_volume():
    reachable = {volume_to_speed(v / 100) for v in range(101)}
    assert reachable == set(range(MIN_SPEED, MAX_SPEED + 1))

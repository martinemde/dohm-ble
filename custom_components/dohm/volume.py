"""Mapping between Home Assistant volume levels and Dohm speeds.

Kept free of Home Assistant imports so the rounding rules can be tested
without a full Home Assistant install, like the rest of the vendored engine.

Home Assistant carries volume as a float in 0.0..1.0; the Dohm accepts an
integer speed in MIN_SPEED..MAX_SPEED. These helpers map active speeds only.
Entity platforms handle volume/percentage 0 by sending a separate power-off
command, while retaining the last hardware speed.
"""

from __future__ import annotations

import math

from .const import MAX_SPEED, MIN_SPEED


def speed_to_volume(speed: int) -> float:
    """Convert a device speed to the volume level to report."""
    return speed / MAX_SPEED


def volume_to_speed(volume: float) -> int:
    """Convert a requested volume level to the nearest usable device speed.

    Round to the nearest level, with halfway values rounding up, and clamp
    to MIN_SPEED..MAX_SPEED. This helper does not control power: callers must
    handle zero with a power-off command instead of sending the clamped speed.
    """
    return min(MAX_SPEED, max(MIN_SPEED, math.floor(volume * MAX_SPEED + 0.5)))

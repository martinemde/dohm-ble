"""Mapping between Home Assistant volume levels and Dohm speeds.

Kept free of Home Assistant imports so the rounding rules can be tested
without a full Home Assistant install, like the rest of the vendored engine.

Home Assistant carries volume as a float in 0.0..1.0; the Dohm accepts an
integer speed in MIN_SPEED..MAX_SPEED. The device has no silent setting, so
there is no speed that corresponds to a volume of 0.
"""

from __future__ import annotations

import math

from .const import MAX_SPEED, MIN_SPEED


def speed_to_volume(speed: int) -> float:
    """Convert a device speed to the volume level to report."""
    return speed / MAX_SPEED


def volume_to_speed(volume: float) -> int:
    """Convert a requested volume level to the nearest usable device speed.

    Rounds up so that dragging the slider anywhere inside a step lands on that
    step, and clamps: the slider reaches 0.0 but the device's quietest setting
    is MIN_SPEED, and a media player at volume 0 is still playing (turning it
    off is a separate command).
    """
    return min(MAX_SPEED, max(MIN_SPEED, math.ceil(volume * MAX_SPEED)))

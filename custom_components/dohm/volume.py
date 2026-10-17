"""Mapping between Home Assistant volume levels and Dohm speeds.

Kept free of Home Assistant imports so the rounding rules can be tested
without a full Home Assistant install, like the rest of the vendored engine.

Home Assistant carries volume as a float in 0.0..1.0; the Dohm accepts an
integer speed in 1..MAX_SPEED. A rounded volume of zero represents off:
callers send a separate power-off command and retain the last hardware speed.
"""

from __future__ import annotations

import math

from .const import MAX_SPEED


def speed_to_volume(speed: int) -> float:
    """Convert a device speed to the volume level to report."""
    return speed / MAX_SPEED


def volume_to_speed(volume: float) -> int:
    """Convert a requested volume level to the nearest usable device speed.

    Round to the nearest level, with halfway values rounding up, and clamp
    to 0..MAX_SPEED. A result of zero means off, not a writable device speed:
    callers must send a power-off command and preserve the last active speed.
    """
    return min(MAX_SPEED, max(0, math.floor(volume * MAX_SPEED + 0.5)))

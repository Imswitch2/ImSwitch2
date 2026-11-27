"""Bringing a stopped scan's outputs to rest (plan scan-stop-now).

A Stop ends a scan in the middle of its waveform: the scanners hold the
sample they stopped at. They are brought back to where the scan started,
no faster than their positioners declare they can move -- the same
trapezoidal velocity profile the galvo designer positions by.
"""

import math

import numpy as np

from imswitch.imcontrol.model.scan_parameters import samples_for_duration


def rest_to_rest_ramp(start, end, v_max, a_max, rate_hz) -> np.ndarray:
    """The fastest move from ``start`` to ``end`` that starts and ends at
    rest and never exceeds speed ``v_max`` or acceleration ``a_max``, sampled
    at ``rate_hz``: accelerate, cruise, decelerate (a triangle when the move
    is too short to reach ``v_max``). Positions in any one unit, ``v_max``
    per second and ``a_max`` per second squared in it. The samples are the
    positions one sample period apart, the first one period after ``start``
    (which is not repeated), the last exactly ``end``; a move of no distance
    is no samples."""
    distance = float(end) - float(start)
    length = abs(distance)
    if length == 0.0:
        return np.empty(0)
    speed, accel = float(v_max), float(a_max)
    if speed <= 0.0 or accel <= 0.0:
        raise ValueError(f'A ramp needs a positive speed and acceleration, not {v_max!r} '
                         f'and {a_max!r}.')
    t_acc = speed / accel
    d_acc = 0.5 * accel * t_acc ** 2
    if 2.0 * d_acc >= length:          # never reaches v_max: a triangle
        t_acc = math.sqrt(length / accel)
        d_acc = 0.5 * length
        speed = accel * t_acc
        t_cruise = 0.0
    else:
        t_cruise = (length - 2.0 * d_acc) / speed
    total = 2.0 * t_acc + t_cruise
    count = max(1, samples_for_duration(total, rate_hz, 'ceil'))
    t = np.minimum(np.arange(1, count + 1) / float(rate_hz), total)
    travelled = np.where(
        t <= t_acc, 0.5 * accel * t ** 2,
        np.where(t <= t_acc + t_cruise, d_acc + speed * (t - t_acc),
                 length - 0.5 * accel * (total - t) ** 2))
    travelled[-1] = length
    return float(start) + math.copysign(1.0, distance) * travelled


def axis_limits_volts(managerProperties):
    """A positioner's declared ``vel_max`` (µm/µs) and ``acc_max`` (µm/µs²) in
    volts per second and per second squared, through its ``conversionFactor``
    (µm per volt), or ``None`` when it does not declare all three."""
    props = managerProperties or {}
    try:
        v_max, a_max = float(props['vel_max']), float(props['acc_max'])
        um_per_volt = float(props['conversionFactor'])
    except (KeyError, TypeError, ValueError):
        return None
    if v_max <= 0.0 or a_max <= 0.0 or um_per_volt == 0.0:
        return None
    um_per_volt = abs(um_per_volt)
    return v_max * 1e6 / um_per_volt, a_max * 1e12 / um_per_volt


__all__ = ['axis_limits_volts', 'rest_to_rest_ramp']


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

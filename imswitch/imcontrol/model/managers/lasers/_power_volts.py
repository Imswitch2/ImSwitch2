"""A laser's power as the volts on its analog channel: one conversion for its
manager and for every scan engine that plays a power level inside a step
(docs/design/plans/scan-engines-and-expert-scan.md §7.8).

With a calibration (``managerProperties.calibCsvPath``: volts in the first
column, the power they give in the second) a value is a percentage of the
calibrated range; without one the value IS the voltage. That is how
``NidaqLaserManager`` sets a laser by hand, so a scan drives it the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.interpolate import interp1d

#: Percent: the calibrated power is scaled to 0..100.
_PERCENT = 100.0


@dataclass(frozen=True)
class PowerVolts:
    """``volts(value)``; ``unit`` of the value; ``source`` of the conversion.
    A value outside the calibration gives NaN."""
    volts: Callable
    unit: str
    source: str


def calibration_lut(calib_csv_path) -> Callable:
    """Percent of the calibrated power range to volts, from a calibration
    file (volts, power per row); NaN outside it."""
    data = np.loadtxt(calib_csv_path)
    data[:, 1] -= data[:, 1].min()
    data[:, 1] /= data[:, 1].max() / _PERCENT
    return interp1d(data[:, 1], data[:, 0], bounds_error=False)


def power_volts(name, laserInfo) -> PowerVolts:
    props = getattr(laserInfo, 'managerProperties', None) or {}
    path = props.get('calibCsvPath')
    if path:
        lut = calibration_lut(path)
        return PowerVolts(lambda value: np.asarray(lut(value), dtype=float), '%',
                          f'{name}.calibCsvPath')
    return PowerVolts(lambda value: np.asarray(value, dtype=float), 'V',
                      f'{name}: no calibration, so the value is the voltage')


def level_volts(name, laserInfo, values):
    """``(volts, refusal)`` for power ``values`` of laser ``name`` inside a
    scan: refused, with the numbers, when one is outside the laser's value
    range or its calibration."""
    values = np.asarray(values, dtype=float)
    low = getattr(laserInfo, 'valueRangeMin', None)
    high = getattr(laserInfo, 'valueRangeMax', None)
    conversion = power_volts(name, laserInfo)
    if values.size and ((low is not None and float(values.min()) < float(low))
                        or (high is not None and float(values.max()) > float(high))):
        bounds = (f'{"-inf" if low is None else f"{float(low):g}"} to '
                  f'{"inf" if high is None else f"{float(high):g}"}')
        return None, (f'{name} takes {bounds} {conversion.unit}; the scan asks '
                      f'{float(values.min()):g} to {float(values.max()):g}.')
    volts = conversion.volts(values)
    if np.isnan(volts).any():
        return None, (f'{name}: a power the scan asks is outside its calibration '
                      f'({conversion.source}).')
    return volts, ''


__all__ = ['PowerVolts', 'calibration_lut', 'level_volts', 'power_volts']


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

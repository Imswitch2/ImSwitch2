"""A laser's power as the volts on its analog channel: one conversion for its
manager and for every scan engine that plays a power level inside a step
(docs/design/plans/scan-engines-and-expert-scan.md §7.8).

With a power calibration in force on the laser (a ``LaserPowerTable``,
``imswitch.imcontrol.model.calibrations``) a value is a percentage, 0-100 %,
converted by that table exactly as the laser's manager converts it by hand;
without one the value IS the voltage. The calibration is the manager's
(``powerCalibration``: ``calibrationId`` or ``calibCsvPath``, whatever loaded,
and a swap made in the Calibrations window), handed in by the caller; only a
caller that has no laser managers (an engine built on its own, in tests)
resolves the setup's reference itself, through the same resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from imswitch.imcontrol.model.calibrations import (
    CalibrationRangeError,
    LaserPowerTable,
    resolve_laser_calibration,
)

#: The range of a value under a calibration.
_PERCENT = (0.0, 100.0)


@dataclass(frozen=True)
class PowerVolts:
    """``volts(value)``; ``unit`` of the value; ``source`` of the conversion.
    Under a calibration ``volts`` raises ``CalibrationRangeError`` for a value
    outside it (it never returns NaN or a clamped drive)."""
    volts: Callable
    unit: str
    source: str
    calibration: Optional[LaserPowerTable] = None


def setup_calibration(name, laserInfo) -> Optional[LaserPowerTable]:
    """The calibration the setup's ``calibrationId`` / ``calibCsvPath`` names,
    resolved as a laser manager resolves it; None (volts) when there is none
    or it cannot be used. For callers without the laser's manager."""
    props = getattr(laserInfo, 'managerProperties', None) or {}
    resolution = resolve_laser_calibration(
        laser_name=name, drive_unit='V',
        calibration_id=props.get('calibrationId'),
        calib_csv_path=props.get('calibCsvPath'),
        wavelength_nm=getattr(laserInfo, 'wavelength', None))
    return resolution.table


_FROM_SETUP = object()


def power_volts(name, laserInfo, calibration=_FROM_SETUP) -> PowerVolts:
    """The conversion for laser ``name``. ``calibration``: the table in force
    on its manager (``None``: it runs on volts); left out, the setup's
    reference is resolved here (:func:`setup_calibration`)."""
    if calibration is _FROM_SETUP:
        calibration = setup_calibration(name, laserInfo)
    if calibration is not None:
        def volts(value):
            values = np.asarray(value, dtype=float)
            return np.asarray(calibration.drive_for_percent_array(np.atleast_1d(values)),
                              dtype=float).reshape(values.shape)
        return PowerVolts(volts, '%', f'{name}: calibration {calibration.id}', calibration)
    return PowerVolts(lambda value: np.asarray(value, dtype=float), 'V',
                      f'{name}: no calibration, so the value is the voltage')


def level_volts(name, laserInfo, values, calibration=_FROM_SETUP):
    """``(volts, refusal)`` for power ``values`` of laser ``name`` inside a
    scan: refused, with the numbers, when one is outside the laser's range
    (0-100 % under a calibration, its value range in volts otherwise) or its
    calibration."""
    values = np.asarray(values, dtype=float)
    conversion = power_volts(name, laserInfo, calibration)
    if conversion.calibration is not None:
        low, high = _PERCENT
    else:
        low = getattr(laserInfo, 'valueRangeMin', None)
        high = getattr(laserInfo, 'valueRangeMax', None)
    if values.size and ((low is not None and float(values.min()) < float(low))
                        or (high is not None and float(values.max()) > float(high))):
        bounds = (f'{"-inf" if low is None else f"{float(low):g}"} to '
                  f'{"inf" if high is None else f"{float(high):g}"}')
        return None, (f'{name} takes {bounds} {conversion.unit}; the scan asks '
                      f'{float(values.min()):g} to {float(values.max()):g}.')
    try:
        volts = conversion.volts(values)
    except CalibrationRangeError as error:
        return None, (f'{name}: a power the scan asks is outside its calibration '
                      f'({conversion.source}: {error}).')
    if np.isnan(volts).any():
        return None, f'{name}: a power the scan asks gives no voltage ({conversion.source}).'
    return volts, ''


def laser_calibration_kwargs(lookup, name) -> dict:
    """``{'calibration': lookup(name)}`` for :func:`level_volts`, or ``{}``
    when the caller has no lookup (the setup's reference is then resolved)."""
    return {} if not callable(lookup) else {'calibration': lookup(name)}


__all__ = ['PowerVolts', 'laser_calibration_kwargs', 'level_volts', 'power_volts',
           'setup_calibration']


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

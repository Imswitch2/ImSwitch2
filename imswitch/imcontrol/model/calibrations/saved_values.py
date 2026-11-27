"""One rule for a laser value saved earlier and applied now: presets, setup
modes, restored widget state and replay of a recording all go through
:func:`decide_saved_value`.

The saved value was set under some calibration (or on raw units); the laser
now runs under the one in force. :func:`compare_calibration_identity` says
whether they are the same:

``same``      the value means what it meant: apply it as saved.
``different`` never reinterpret the saved number through the calibration in
              force. Apply the recorded *drive* instead, converted into the
              current scale (a percent through the current calibration, or
              the drive itself on raw units) -- it reproduces the output the
              value was saved with, so it cannot overshoot it. When no drive
              was recorded, or it lies outside the current calibration or the
              laser's range, skip the value.
``unknown``   the source predates calibration provenance: apply it as saved
              (as before), except when its recorded unit differs from the
              laser's current one, or no unit was recorded and the laser's
              referenced calibration failed to load -- a percentage would
              then be taken as a raw drive, so skip.

Pure: no Qt, no managers. Callers pass the calibration in force (a
``LaserPowerTable`` or None) and the laser's raw range and units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .laser_power import LaserPowerTable
from .matching import DIFFERENT, SAME, IdentityComparison, compare_calibration_identity

#: Apply the saved value unchanged (same calibration).
APPLY_AS_SAVED = 'same'
#: Apply the saved value unchanged; what it meant cannot be verified.
APPLY_UNVERIFIED = 'unverified'
#: Apply the recorded drive, converted into the current scale.
APPLY_BY_DRIVE = 'by_drive'
#: Do not apply.
SKIP_VALUE = 'skip'


@dataclass(frozen=True)
class SavedValueDecision:
    outcome: str
    value: Optional[float]          # in the laser's current scale; None = skip
    drive: Optional[float]          # the recorded drive used (APPLY_BY_DRIVE)
    reason: str                     # '' for APPLY_AS_SAVED
    comparison: IdentityComparison

    @property
    def applies(self) -> bool:
        return self.value is not None


def recorded_calibration_known(saved: Mapping[str, Any], *, calibration_key='calibration',
                               units_key='valueUnits') -> bool:
    """Whether ``saved`` says which calibration its value was set under: the
    calibration slot is present, or its unit is recorded and is not percent
    (a value recorded in the laser's own unit was set on raw units)."""
    if calibration_key in saved:
        return True
    units = saved.get(units_key)
    return isinstance(units, str) and units.strip() not in ('', '%')


def decide_saved_value(value: Any, *, recorded: Any, recorded_known: bool,
                       table: Optional[LaserPowerTable],
                       raw_range: Tuple[Optional[float], Optional[float]] = (None, None),
                       raw_units: str = '', current_units: str = '',
                       saved_units: Optional[str] = None,
                       calibration_failed: bool = False) -> SavedValueDecision:
    """What to apply for a saved laser ``value`` (see the module docstring).

    ``recorded`` is the saved ``{id, contentHash, driveValue}`` (None: saved
    on raw units) and ``recorded_known`` whether anything was recorded at
    all. ``table`` is the calibration in force; ``raw_range`` / ``raw_units``
    the laser's own drive range and unit; ``current_units`` the unit its value
    is in now ('%' under a calibration); ``saved_units`` the unit the value
    was saved in, when known."""
    active = table.identity(None) if table is not None else None
    comparison = compare_calibration_identity(recorded, active, recorded_known=recorded_known)
    if comparison.status == SAME:
        return SavedValueDecision(APPLY_AS_SAVED, value, None, '', comparison)
    if comparison.status != DIFFERENT:
        if isinstance(saved_units, str) and isinstance(current_units, str) \
                and saved_units and current_units and saved_units != current_units:
            return SavedValueDecision(
                SKIP_VALUE, None, None,
                f'saved as {value} {saved_units}, but the laser now takes {current_units}; '
                f'not applied.', comparison)
        if calibration_failed and not saved_units:
            return SavedValueDecision(
                SKIP_VALUE, None, None,
                f'{value} was saved without a unit and the laser\'s calibration did not '
                f'load, so it now takes raw {raw_units}; not applied.', comparison)
        reason = '' if table is None else (
            f'{comparison.message} Calibration {table.id} is in force now, so what the '
            f'stored number meant cannot be verified.')
        return SavedValueDecision(APPLY_UNVERIFIED, value, None, reason, comparison)
    drive = recorded.get('driveValue') if isinstance(recorded, Mapping) else None
    if recorded is None:
        drive = value       # saved on raw units: the value was the drive
    try:
        drive = None if drive is None else float(drive)
    except (TypeError, ValueError):
        drive = None
    if drive is None:
        return SavedValueDecision(
            SKIP_VALUE, None, None,
            f'{comparison.message} No drive value was recorded, so {value} cannot be '
            f'applied safely; not applied.', comparison)
    low, high = raw_range
    if table is not None:
        converted = table.percent_for_drive(drive)
        scaleText = 'the current calibration'
    else:
        inside = ((low is None or drive >= float(low)) and (high is None or drive <= float(high)))
        converted = drive if inside else None
        scaleText = f'the laser\'s range ({low}-{high} {raw_units})'
    if converted is None:
        return SavedValueDecision(
            SKIP_VALUE, None, drive,
            f'{comparison.message} The saved drive {drive:g} {raw_units} is outside '
            f'{scaleText}; not applied.', comparison)
    return SavedValueDecision(
        APPLY_BY_DRIVE, converted, drive,
        f'{comparison.message} Applied the saved drive {drive:g} {raw_units} (now '
        f'{converted:.4g} {current_units}) instead of reinterpreting {value} through the '
        f'calibration in force.', comparison)


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

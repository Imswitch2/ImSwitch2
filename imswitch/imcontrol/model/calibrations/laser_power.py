"""Applying a laser power calibration: percent → raw drive, and the absolute
power behind a setpoint.

**Percent semantics are the legacy ones, exactly.** The measured power is
shifted so its minimum is 0 and scaled so its maximum is 100 %, and the
percent → drive map is ``scipy.interpolate.interp1d(percent, drive)`` over
the rows in their file order -- the arithmetic the two removed copies of
``create_lut_from_calib`` (``NidaqLaserManager``, ``AAAOTFLaserManager``)
performed. A saved "40 %" therefore drives the laser exactly as before. One
deliberate difference: 100 % is always reachable. Before, ``max / (max *
0.01)`` could round to 99.99999999999999, and a request for 100 % fell
outside the table and was refused; it now gives the drive at the top of the
table. Every value the old table accepted converts bit for bit as before.

**Outside the measured range a request is refused** with
:class:`CalibrationRangeError` -- never NaN, never clamped.

**Absolute power** is shown, not set: when the record's power has a unit
(W, mW, ...), ``power_for_percent`` gives the optical power the curve
predicts for a setpoint (``min + p/100 · (max − min)``, which is what the
interpolation inverts), so the laser control can say "12.4 mW at the back
focal plane".
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

import numpy as np
from scipy.interpolate import interp1d

from .record import (
    KIND_LASER_POWER,
    RAW_UNITS_ID,
    Calibration,
    CalibrationError,
    CalibrationInvalid,
    CalibrationRangeError,
)

#: ``CalibrationResolution.requested`` of a manager that does not apply
#: calibrations although its setup names a ``calibCsvPath``.
UNSUPPORTED_CALIB_CSV_PATH = 'calibCsvPath (not applied by this manager)'

_POWER_SCALE_W = {'W': 1.0, 'mW': 1e-3, 'uW': 1e-6, 'nW': 1e-9}


def validate_laser_power(record: Calibration, *, drive_unit: str = '') -> List[str]:
    """Why ``record`` cannot drive a laser (empty list: it can).

    ``drive_unit`` is the laser's raw unit; a record that names a different
    drive unit is refused -- its numbers would mean something else."""
    problems: List[str] = []
    if record.kind != KIND_LASER_POWER:
        return [f'it is a {record.kind!r} calibration, not a laser power calibration']
    table = record.table
    if len(table) < 2:
        return [f'{len(table)} row(s); a power calibration needs at least two']
    try:
        rows = np.asarray([row[:2] for row in table], dtype=float)
    except (TypeError, ValueError):
        return ['rows are not numbers']
    if rows.ndim != 2 or rows.shape[1] < 2:
        return ['rows need a drive and a power column']
    if not np.all(np.isfinite(rows)):
        return ['non-finite drive or power values']
    drive, power = rows[:, 0], rows[:, 1]
    if float(power.max()) <= float(power.min()):
        problems.append('the measured power is constant: the curve has no span to scale to 0-100 %')
    if np.unique(drive).size != drive.size:
        problems.append('the same drive value appears more than once (keep one row per drive '
                        'value, or import the file with "Fit a monotonic curve")')
    order = np.argsort(drive, kind='stable')
    falling = np.nonzero(np.diff(power[order]) < 0)[0]
    if falling.size:
        i = int(falling[0])
        problems.append(
            f'power falls from {power[order][i]:g} to {power[order][i + 1]:g} as the drive '
            f'rises from {drive[order][i]:g} to {drive[order][i + 1]:g}: the curve is not '
            f'monotonic, so a percentage would not name one drive value. Remove or re-measure '
            f'the reversing points, or import the file with "Fit a monotonic curve" (the '
            f'isotonic fit measureLaserPowerLut uses, recorded as fitted on import)')
    unit = (record.input.unit or '').strip()
    if drive_unit and unit and unit != drive_unit:
        problems.append(
            f'it maps a drive in {unit!r}, but this laser is driven in {drive_unit!r}')
    if record.extrapolation != 'refuse':
        problems.append(f'unknown extrapolation policy {record.extrapolation!r}')
    return problems


class LaserPowerTable:
    """A validated laser power calibration, ready to convert values.

    Raises :class:`CalibrationInvalid` when :func:`validate_laser_power`
    finds a problem."""

    def __init__(self, record: Calibration, *, drive_unit: str = '', source: str = ''):
        problems = validate_laser_power(record, drive_unit=drive_unit)
        if problems:
            raise CalibrationInvalid('; '.join(problems))
        self.record = record
        self.source = source or record.provenance.sourceFile or record.id
        self.driveUnit = record.input.unit or drive_unit
        self.powerUnit = record.output.unit
        data = np.asarray([row[:2] for row in record.table], dtype=float)
        # --- legacy create_lut_from_calib, verbatim -----------------------
        percent = data[:, 1].copy()
        percent -= percent.min()
        percent /= percent.max() * 0.01
        self._lut = interp1d(percent, data[:, 0], bounds_error=False)
        # ------------------------------------------------------------------
        # max / (max * 0.01) can round to 99.99999999999999; the gap up to
        # 100 % maps to the drive at the top of the table instead of being
        # refused. Every value the legacy table accepted is untouched.
        self._percentTop = float(percent.max())
        self._driveTop = float(self._lut(self._percentTop))
        self._powerMin = float(data[:, 1].min())
        self._powerMax = float(data[:, 1].max())
        order = np.argsort(data[:, 0], kind='stable')
        self._driveSorted = data[order, 0]
        self._percentByDrive = percent[order]
        self.driveMin = float(self._driveSorted[0])
        self.driveMax = float(self._driveSorted[-1])

    # -- identity ---------------------------------------------------------

    @property
    def id(self) -> str:
        return self.record.id

    def contentHash(self) -> str:
        return self.record.contentHash()

    def identity(self, drive_value: Optional[float]) -> dict:
        """The provenance slot written next to a laser's value:
        ``{'id', 'contentHash', 'driveValue'}``."""
        return {'id': self.id, 'contentHash': self.contentHash(),
                'driveValue': None if drive_value is None else float(drive_value)}

    # -- conversions -------------------------------------------------------

    def drive_for_percent(self, percent) -> float:
        """The raw drive for a percent setpoint; refuses outside 0-100 % or
        outside the table."""
        try:
            value = float(percent)
        except (TypeError, ValueError):
            raise CalibrationRangeError(f'{percent!r} is not a number') from None
        if not math.isfinite(value) or value < 0.0 or value > 100.0:
            raise CalibrationRangeError(
                f'{percent!r} % is outside the calibrated range 0-100 %')
        drive = self._driveTop if value > self._percentTop else float(self._lut(value))
        if not math.isfinite(drive):
            raise CalibrationRangeError(
                f'{percent!r} % is outside the calibration table ({self.source})')
        return drive

    def drive_for_percent_array(self, values: Iterable[float]) -> np.ndarray:
        """Vectorised :meth:`drive_for_percent` (for scan engines that play a
        list of power levels); refuses the whole array if one value is out."""
        values = np.asarray(values, dtype=float)
        if values.size and (not np.all(np.isfinite(values))
                            or float(values.min()) < 0.0 or float(values.max()) > 100.0):
            raise CalibrationRangeError(
                f'requested {float(np.nanmin(values)):g} to {float(np.nanmax(values)):g} %; '
                f'the calibration covers 0-100 %')
        drives = np.asarray(self._lut(values), dtype=float)
        drives = np.where(values > self._percentTop, self._driveTop, drives)
        if not np.all(np.isfinite(drives)):
            raise CalibrationRangeError(f'a requested power is outside {self.source}')
        return drives

    def percent_for_drive(self, drive) -> Optional[float]:
        """The percent setpoint that gives ``drive``; None outside the table."""
        try:
            value = float(drive)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < self.driveMin or value > self.driveMax:
            return None
        return min(100.0, float(np.interp(value, self._driveSorted, self._percentByDrive)))

    @property
    def hasAbsolutePower(self) -> bool:
        return self.powerUnit in _POWER_SCALE_W

    def power_for_percent(self, percent) -> Optional[float]:
        """Predicted optical power (in :attr:`powerUnit`) at a setpoint, or
        None (no absolute unit, or outside 0-100 %)."""
        if not self.hasAbsolutePower:
            return None
        try:
            value = float(percent)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0.0 or value > 100.0:
            return None
        return self._powerMin + value / 100.0 * (self._powerMax - self._powerMin)

    def format_power(self, power: Optional[float]) -> str:
        return format_power(power, self.powerUnit)


def fit_monotone(record: Calibration):
    """``(fitted record, largest correction)``: power fitted as a monotone
    non-decreasing function of the drive with the isotonic fit of
    ``imcommon/algorithms/power_lut.py`` (repeated drive values averaged
    first), plateaus collapsed to their lowest drive -- what
    ``measureLaserPowerLut`` exports -- and recorded in the provenance as
    fitted on import. The largest correction is in the record's power unit;
    the caller shows it before anything is stored."""
    from dataclasses import replace

    from imswitch.imcommon.algorithms.power_lut import isotonic_increasing

    rows = np.asarray([row[:2] for row in record.table], dtype=float)
    if rows.ndim != 2 or rows.shape[0] < 2 or not np.all(np.isfinite(rows)):
        raise CalibrationInvalid('the rows cannot be fitted (fewer than two, or non-finite)')
    drives, inverse, counts = np.unique(rows[:, 0], return_inverse=True, return_counts=True)
    power = np.bincount(inverse, weights=rows[:, 1]) / counts
    fitted = isotonic_increasing(power, counts)
    correction = float(np.max(np.abs(fitted - power))) if power.size else 0.0
    keep = np.ones(drives.size, dtype=bool)
    keep[1:] = fitted[1:] > fitted[:-1]
    table = [[float(d), float(p)] for d, p in zip(drives[keep], fitted[keep])]
    if len(table) < 2:
        raise CalibrationInvalid('after the fit the power is constant: nothing to calibrate')
    columns = (record.columns or ['drive', 'power'])[:2]
    header = tuple(record.provenance.header) + (
        ('fitted on import', f'isotonic fit of {len(rows)} rows to {len(table)}, largest '
                             f'correction {correction:.4g} {record.output.unit}'.rstrip()),)
    fittedRecord = replace(
        record, data={'columns': columns, 'table': table},
        validRange=(table[0][0], table[-1][0]),
        provenance=replace(record.provenance, header=header))
    return fittedRecord, correction


def format_power(power: Optional[float], unit: str) -> str:
    """``0.0124 W`` → ``'12.4 mW'`` (three significant digits)."""
    if power is None or unit not in _POWER_SCALE_W:
        return ''
    watts = float(power) * _POWER_SCALE_W[unit]
    if watts == 0.0:
        return '0 W'
    for name, scale in (('W', 1.0), ('mW', 1e-3), ('µW', 1e-6), ('nW', 1e-9)):
        if abs(watts) >= scale or name == 'nW':
            return f'{watts / scale:.3g} {name}'
    return f'{watts:.3g} W'


@dataclass(frozen=True)
class CalibrationResolution:
    """What a laser runs on, and why.

    ``state``: ``'calibrated'`` (``table`` in force), ``'raw'`` (none
    requested, or explicitly raw), ``'failed'`` (one was referenced but cannot
    be used: ``error`` says why; the laser runs on raw units)."""
    state: str
    table: Optional[LaserPowerTable] = None
    requested: str = ''
    resolvedPath: str = ''
    error: str = ''

    @property
    def failed(self) -> bool:
        return self.state == 'failed'


def resolve_laser_calibration(*, laser_name: str, drive_unit: str,
                              calibration_id=None, calib_csv_path=None,
                              wavelength_nm=None, store=None) -> CalibrationResolution:
    """Resolve what a setup asks for into a usable calibration (or raw units).

    Precedence: ``calibrationId`` (a record in the global store, or ``'raw'``)
    over ``calibCsvPath`` (a file used as it is, a relative path resolving
    against the working directory as it always did). Empty values count as
    absent -- the config editor seeds ``calibCsvPath: ""``."""
    calibration_id = str(calibration_id or '').strip()
    csv_path = str(calib_csv_path or '').strip()
    if calibration_id == RAW_UNITS_ID:
        return CalibrationResolution('raw', requested='raw units (calibrationId: raw)')
    if calibration_id:
        from .store import CalibrationStore

        store = store if store is not None else CalibrationStore()
        requested = f'calibration {calibration_id}'
        try:
            stored = store.get(calibration_id)
        except CalibrationError as exc:
            return CalibrationResolution('failed', requested=requested, error=str(exc))
        if stored is None:
            return CalibrationResolution(
                'failed', requested=requested,
                error=f'no calibration with id {calibration_id!r} in {store.root}')
        try:
            table = LaserPowerTable(stored.record, drive_unit=drive_unit, source=str(stored.path))
        except CalibrationError as exc:
            return CalibrationResolution('failed', requested=requested,
                                         resolvedPath=str(stored.path),
                                         error=f'{calibration_id}: {exc}')
        return CalibrationResolution('calibrated', table=table, requested=requested,
                                     resolvedPath=str(stored.path))
    if csv_path:
        from .legacy_csv import read_legacy_csv

        resolved = os.path.abspath(csv_path)
        requested = f'calibCsvPath {csv_path}'
        if not os.path.isfile(resolved):
            return CalibrationResolution(
                'failed', requested=requested, resolvedPath=resolved,
                error=f'calibCsvPath file not found: {resolved}'
                      + ('' if os.path.isabs(csv_path) else
                         f' (relative paths resolve against the working directory, '
                         f'{os.getcwd()})'))
        try:
            record = read_legacy_csv(resolved, laser_name=laser_name, drive_unit=drive_unit,
                                     wavelength_nm=wavelength_nm,
                                     record_id=f'csv:{os.path.basename(resolved)}')
            table = LaserPowerTable(record, drive_unit=drive_unit, source=resolved)
        except CalibrationError as exc:
            return CalibrationResolution('failed', requested=requested, resolvedPath=resolved,
                                         error=f'{resolved}: {exc}')
        return CalibrationResolution('calibrated', table=table, requested=requested,
                                     resolvedPath=resolved)
    return CalibrationResolution('raw', requested='')


# -- reading a laser manager (duck-typed: test doubles and plugins too) --------

def power_table_of(manager) -> Optional[LaserPowerTable]:
    """The calibration in force on ``manager`` (None: raw units, or a
    manager that knows nothing of calibrations)."""
    table = getattr(manager, 'powerCalibration', None)
    return table if isinstance(table, LaserPowerTable) else None


def resolution_of(manager) -> CalibrationResolution:
    """What ``manager`` runs on; a ``calibCsvPath`` naming a file on a
    manager that does not apply calibrations counts as failed (reported)."""
    resolve = getattr(manager, 'powerCalibrationResolution', None)
    resolution = resolve() if callable(resolve) else None
    if not isinstance(resolution, CalibrationResolution):
        return CalibrationResolution('raw')
    if resolution.requested == UNSUPPORTED_CALIB_CSV_PATH:
        path = str(manager.getProperty('calibCsvPath') or '').strip()
        if not path:
            return CalibrationResolution('raw')
        return CalibrationResolution(
            'failed', requested=f'calibCsvPath {path}',
            error=f'{type(manager).__name__} does not apply power calibrations; this '
                  f'laser runs on raw units ({manager.rawValueUnits}).')
    return resolution


def laser_value_scale(manager):
    """``(min, max, units, decimals, step)`` of the value a laser takes now:
    0-100 % while a calibration is in force, its own range and unit
    otherwise."""
    usesCalibration = getattr(manager, 'usesCalibrationLookup', None)
    if callable(usesCalibration) and usesCalibration():
        return 0, 100, '%', 1, (0.5 if getattr(manager, 'valueRangeStep', None) is not None
                                 else None)
    return (getattr(manager, 'valueRangeMin', None), getattr(manager, 'valueRangeMax', None),
            getattr(manager, 'valueUnits', ''), getattr(manager, 'valueDecimals', None),
            getattr(manager, 'valueRangeStep', None))


def raw_units_of(manager) -> str:
    """The unit of a laser's drive whatever is in force."""
    units = getattr(manager, 'rawValueUnits', None)
    if not isinstance(units, str):
        units = getattr(manager, 'valueUnits', '')
    return units if isinstance(units, str) else ''


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

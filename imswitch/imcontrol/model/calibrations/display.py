"""Texts shown for a laser's calibration: the laser control's tooltip and the
one-line status. Pure functions so tests can read exactly what the operator
sees."""
from __future__ import annotations

from typing import Optional

from .laser_power import CalibrationResolution


def _fmt(value, decimals: int = 3) -> str:
    try:
        return f'{float(value):.{decimals}g}'
    except (TypeError, ValueError):
        return str(value)


def raw_units_text(units: str, valueRange=None) -> str:
    span = ''
    if valueRange is not None and None not in tuple(valueRange):
        span = f', {_fmt(valueRange[0])}-{_fmt(valueRange[1])} {units}'
    return f'the value is the raw drive in {units or "the manager’s units"}{span}'


def laser_calibration_tooltip(resolution: CalibrationResolution, value, *, rawUnits: str,
                              rawRange=None) -> str:
    """The laser control's tooltip for the current setpoint ``value``.

    Calibrated: the optical power the curve predicts (when the calibration
    has a power unit) and where it was measured, the drive behind the
    setpoint, the calibration's name, date and comment. Otherwise: that the
    laser runs on raw units, and why when a referenced calibration failed."""
    table = resolution.table
    if resolution.state == 'calibrated' and table is not None:
        record = table.record
        lines = []
        power = table.power_for_percent(value)
        where = record.measuredAt or 'location not recorded'
        if power is not None:
            lines.append(f'{table.format_power(power)} at {where}')
        else:
            lines.append(f'{_fmt(value)} % of the measured span '
                         f'(the calibration has no power unit; measured at {where})')
        try:
            drive = table.drive_for_percent(value)
            lines.append(f'Drive: {_fmt(drive, 4)} {table.driveUnit}'.rstrip())
        except Exception:  # noqa: BLE001 - outside the table: say so, never guess
            lines.append('Outside the calibrated range 0-100 %')
        if record.comment:
            lines.append(record.comment)
        created = record.provenance.created[:10]
        lines.append(f'Calibration {record.id}'
                     + (f', {created}' if created else '')
                     + f' ({record.shortHash()})')
        return '\n'.join(lines)
    if resolution.failed:
        return (f'Raw units — the calibration did not load: {resolution.error}\n'
                f'Until it is fixed, {raw_units_text(rawUnits, rawRange)}. '
                f'See Hardware → Calibrations….')
    return f'Raw units — no calibration: {raw_units_text(rawUnits, rawRange)}.'


def laser_calibration_status(resolution: CalibrationResolution) -> str:
    """One line for the Calibrations dialog's laser list."""
    if resolution.state == 'calibrated' and resolution.table is not None:
        record = resolution.table.record
        return f'{record.measuredAt or "calibrated"} — {record.id}'
    if resolution.failed:
        return 'raw units — calibration did not load'
    return 'raw units'


def optional_power_text(resolution: CalibrationResolution, value) -> Optional[str]:
    table = resolution.table
    if table is None:
        return None
    power = table.power_for_percent(value)
    return table.format_power(power) if power is not None else None

"""Laser power LUT: a raw-drive sweep measured by a power meter.

Same run mechanism as every other measurement
(``docs/design/plans/transient-instruments-step-scans.md`` §10). The
deployable ``calibCsvPath`` file is an *export* of the run, written only when
the run passes acceptance.

Inside one reservation of laser, meter and waveform outputs:

1. **prepare** — record the laser's value and enabled state; set the meter to
   the laser's wavelength (the readback must agree); switch emission
   **off**; ask the caller to confirm the beam is blocked (a minimum *value*
   is not dark: AOM/AOTF lines leak at drive 0, and other light may share the
   path); zero the meter; measure the dark noise; switch emission back on;
2. **sweep** the raw drive (``applyRawDrive`` — bypasses any loaded LUT, so
   recalibrating a laser that has one is correct);
3. **cleanup** — restore the recorded value, then the enabled state, on every
   exit path; its outcome is reported next to the data, never instead of it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

import numpy as np

from imswitch.imcommon.algorithms.power_lut import LutRefused, write_calib_csv
from imswitch.imcommon.model.measurement_run import MeasurementRunFile
from imswitch.imcommon.model.measurement_run.power_lut import (
    PowerLutResult,
    analyse_power_lut_run,
)

from .controls import RunControl
from .generators import sweep
from .instrument import InstrumentSession
from .runner import CleanupStep, MeasurementRunner, PrepareStep, RunReport, RunSettings

_logger = logging.getLogger(__name__)


class LaserState(Protocol):
    """What the procedure needs from a laser besides its raw drive."""

    name: str
    wavelength_nm: float
    raw_unit: str
    uses_lut: bool

    def read_state(self) -> Tuple[float, bool]:
        """Current (UI value, emission enabled)."""

    def set_enabled(self, enabled: bool, token: Optional[str] = None) -> Optional[bool]:
        """Switch emission; raise on failure. ``False``: the laser has no
        emission switch (nothing sent)."""

    def restore(self, value: float, enabled: bool, token: Optional[str] = None) -> None:
        """Set the value first, then the enabled state."""


@dataclass
class LaserLutSettings:
    drive_values: Sequence[float]
    samples_per_point: int = 5
    settle_s: float = 0.2
    plane_label: str = ''
    notes: str = ''
    wavelength_tolerance_nm: float = 2.0
    max_correction_sigma: float = 3.0
    min_dynamic_range_sigma: float = 20.0
    dark_samples: int = 10
    window_deadline_s: float = 10.0
    move_deadline_s: float = 10.0
    fsync: bool = True
    #: Characterisation only: the result is never exported.
    allow_unverified_timing: bool = False


@dataclass
class LaserLutReport:
    run: Optional[RunReport]
    result: Optional[PowerLutResult]
    lut_file: Optional[Path]
    #: Why no LUT was written (empty when one was).
    refused: List[str] = field(default_factory=list)


class DarkNotConfirmed(RuntimeError):
    """The caller did not confirm that the beam is blocked."""


def run_laser_lut(
    *,
    laser_control: RunControl,
    laser: LaserState,
    meter: InstrumentSession,
    folder: Path,
    settings: LaserLutSettings,
    confirm_dark: Callable[[], bool],
    progress=None,
    executor=None,
) -> LaserLutReport:
    """Measure, accept and export. ``confirm_dark`` is asked with emission off."""
    if not any(q.quantity == 'optical.power' for q in meter.quantities):
        raise ValueError(f'{meter.name} does not measure optical power')
    zero = next((a for a in meter.driver.actions_spec if a.name == 'zero'), None)
    recorded: Dict[str, Any] = {}

    def record_state(token):
        value, enabled = laser.read_state()
        recorded['value'], recorded['enabled'] = float(value), bool(enabled)
        return {'value': recorded['value'], 'enabled': recorded['enabled']}

    def set_wavelength(token):
        applied = meter.set_setting('wavelength_nm', laser.wavelength_nm, owner=token)
        if abs(float(applied) - laser.wavelength_nm) > settings.wavelength_tolerance_nm:
            raise RuntimeError(
                f'{meter.name} cannot be set to {laser.wavelength_nm:g} nm (it applied '
                f'{applied:g} nm): the sensor does not cover this wavelength')
        return {'applied_nm': float(applied)}

    def dark_zero(token):
        switched = laser.set_enabled(False, token)
        if not confirm_dark():
            raise DarkNotConfirmed('the beam was not confirmed blocked; not zeroing')
        zeroed = False
        if zero is not None:
            meter.run_action('zero', confirm_dark=True, owner=token)
            zeroed = True
        boundary = meter.open_window(allow_unverified=settings.allow_unverified_timing,
                                     owner=token)
        window = meter.sample_window(boundary, settings.dark_samples,
                                     settings.window_deadline_s, owner=token)
        values = np.array([s.values['power'] for s in window.samples], float)
        laser.set_enabled(True, token)
        return {
            # False: no emission switch, only the dark confirmation.
            'emission_switched_off': switched is not False,
            'zeroed': zeroed,
            'dark_mean_w': float(values.mean()) if values.size else None,
            'dark_std_w': float(values.std(ddof=1)) if values.size > 1 else None,
            'dark_samples': int(values.size),
        }

    def restore(token):
        if 'value' in recorded:
            laser.restore(recorded['value'], recorded['enabled'], token)

    run_settings = RunSettings(
        samples_per_point=settings.samples_per_point,
        settle_s=settings.settle_s,
        move_deadline_s=settings.move_deadline_s,
        window_deadline_s=settings.window_deadline_s,
        return_to_start=False,            # the restore step owns the laser state
        allow_unverified_timing=settings.allow_unverified_timing,
        plane_label=settings.plane_label,
        illumination={'source': laser.name, 'wavelength_nm': laser.wavelength_nm},
        notes=settings.notes,
        fsync=settings.fsync,
    )
    runner = MeasurementRunner(
        sequence=sweep(laser_control.name, settings.drive_values, purpose='laser power LUT'),
        controls=[laser_control],
        instruments={meter.name: meter},
        folder=folder,
        settings=run_settings,
        owner=f'laser power LUT ({laser.name})',
        progress=progress,
        executor=executor,
        metadata={
            'procedure': 'laser-power-lut',
            'laser': {'name': laser.name, 'wavelength_nm': laser.wavelength_nm,
                      'raw_unit': laser.raw_unit, 'lut_loaded_during_run': bool(laser.uses_lut)},
        },
        prepare=[PrepareStep('laser state', record_state),
                 PrepareStep('meter wavelength', set_wavelength),
                 PrepareStep('dark zero', dark_zero)],
        cleanup_steps=[CleanupStep(f'restore {laser.name}', laser_control.resource, restore)],
    )
    report = runner.run()
    if report.run_file is None:
        return LaserLutReport(report, None, None,
                              [report.detail or report.finalize_error or 'no run file'])

    result = analyse_power_lut_run(
        MeasurementRunFile.load(report.run_file),
        control=laser_control.name, instrument=meter.name,
        wavelength_tolerance_nm=settings.wavelength_tolerance_nm,
        max_correction_sigma=settings.max_correction_sigma,
        min_dynamic_range_sigma=settings.min_dynamic_range_sigma,
    )
    refused = list(result.refused)
    if result.analysis is not None and not result.analysis.accepted:
        refused += result.analysis.reasons
    if refused:
        return LaserLutReport(report, result, None, refused)
    lut_path = Path(folder) / f'{laser.name}_{report.run_id}.calib.csv'
    try:
        write_calib_csv(lut_path, result.analysis, result.header)
    except LutRefused as exc:
        return LaserLutReport(report, result, None, [str(exc)])
    return LaserLutReport(report, result, lut_path, [])

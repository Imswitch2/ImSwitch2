"""Accept a laser-power measurement run and build its LUT.

Run-level acceptance (``docs/design/plans/transient-instruments-step-scans.md``
§10) on top of the curve checks in :mod:`imswitch.imcommon.algorithms.power_lut`:
only a complete run whose every point committed, every command was confirmed,
every sample is valid and acquired with verified timing, and whose meter
wavelength agrees with the declared laser wavelength, can produce a
deployable LUT. Anything less is an exploratory measurement: still in the run
file, never exported.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from imswitch.imcommon.algorithms.power_lut import LutAnalysis, construct_lut

from .hdf5 import MeasurementRunFile
from .types import AcquisitionOutcome


@dataclass
class PowerLutResult:
    analysis: Optional[LutAnalysis]
    #: Why the run cannot produce a LUT (empty = accepted).
    refused: List[str] = field(default_factory=list)
    header: Dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return not self.refused and self.analysis is not None and self.analysis.accepted


def analyse_power_lut_run(
    run: MeasurementRunFile,
    *,
    control: Optional[str] = None,
    instrument: Optional[str] = None,
    wavelength_tolerance_nm: float = 2.0,
    max_correction_sigma: float = 3.0,
    min_dynamic_range_sigma: float = 20.0,
) -> PowerLutResult:
    refused: List[str] = []
    controls = run.control_names()
    control = control or (controls[0] if len(controls) == 1 else None)
    if control is None or control not in controls:
        return PowerLutResult(None, [f'choose the laser control (run has {controls})'])
    meters = run.instruments_with_quantity('optical.power')
    instrument = instrument or (meters[0] if len(meters) == 1 else None)
    if instrument is None or instrument not in meters:
        return PowerLutResult(None, [f'choose the power meter (run has {meters})'])
    inst = run.instruments[instrument]
    spec = inst.quantity('optical.power')
    if spec.unit != 'W':
        return PowerLutResult(None, [f'{instrument} reports power in {spec.unit!r}, expected W'])

    # ---- run-level acceptance
    if run.acquisition is not AcquisitionOutcome.COMPLETE:
        refused.append(f'acquisition is {run.acquisition.value}, not complete')
    committed = run.committed_mask()
    if not committed.all():
        refused.append(f'{int((~committed).sum())} point(s) not committed')
    ok = run.controls[control]['ok']
    if not np.all(ok == 1):
        refused.append(f'{int((ok != 1).sum())} drive command(s) not confirmed')
    windows = inst.windows
    if int(windows['invalid'].sum()):
        refused.append(f'{int(windows["invalid"].sum())} invalid sample(s)')
    samples = inst.samples
    valid = samples[samples['valid'] == 1]
    if len(valid) and not np.all(valid['verified'] == 1):
        refused.append('samples were acquired with unverified timing (exploratory only; '
                       'repeat the sweep with verified timing)')

    meta = run.metadata
    setting_nm = ((meta.get('instruments') or {}).get(instrument, {})
                  .get('settings', {}).get('wavelength_nm'))
    illumination = meta.get('illumination') or {}
    laser_nm = illumination.get('wavelength_nm')
    if laser_nm is None:
        refused.append('the laser wavelength was not declared')
    elif setting_nm is None:
        refused.append('the meter wavelength setting is unknown')
    elif abs(float(setting_nm) - float(laser_nm)) > wavelength_tolerance_nm:
        refused.append(f'meter set to {setting_nm:g} nm, laser is {laser_nm:g} nm')

    # ---- per point statistics, from the raw samples
    drive, mean, std, count = [], [], [], []
    acknowledged = run.controls[control]['acknowledged']
    requested = run.controls[control]['requested']
    for row in range(run.n_points):
        if not committed[row]:
            continue
        block = run.point_samples(instrument, row)
        values = inst.column(block, 'optical.power')
        if values is None or len(values) == 0:
            continue
        a = acknowledged[row]
        drive.append(float(a if math.isfinite(a) else requested[row]))
        mean.append(float(np.mean(values)))
        std.append(float(np.std(values, ddof=1)) if len(values) > 1 else math.nan)
        count.append(len(values))
    dark = (meta.get('preparation') or {}).get('dark zero') or {}
    analysis = construct_lut(
        drive, mean, std, count,
        max_correction_sigma=max_correction_sigma,
        min_dynamic_range_sigma=min_dynamic_range_sigma,
        noise_floor_w=dark.get('dark_std_w'),
    )
    laser = meta.get('laser') or {}
    meter_identity = (meta.get('instruments') or {}).get(instrument, {}).get('identity', {})
    header = {
        'format': 'imswitch calibCsvPath LUT (raw drive -> optical power)',
        'laser': laser.get('name', control),
        'setting unit': laser.get('raw_unit', run.control_unit(control)),
        'laser wavelength nm': laser_nm,
        'meter': f'{meter_identity.get("model", "?")} {meter_identity.get("serial", "")}'.strip(),
        'meter wavelength nm': setting_nm,
        'plane': meta.get('plane_label', ''),
        'zeroed in the dark': bool(dark.get('zeroed', False)),
        'samples per point': (meta.get('settings') or {}).get('samples_per_point'),
        'source run': f'{run.run_id} ({run.path.name})',
        'created': meta.get('created'),
        'imswitch version': meta.get('imswitch_version'),
        'fit correction sigma': round(analysis.correction_sigma, 3)
        if math.isfinite(analysis.correction_sigma) else None,
        'dynamic range sigma': round(analysis.dynamic_range_sigma, 1)
        if math.isfinite(analysis.dynamic_range_sigma) else None,
        'notes': meta.get('notes', ''),
    }
    return PowerLutResult(analysis, refused, header)

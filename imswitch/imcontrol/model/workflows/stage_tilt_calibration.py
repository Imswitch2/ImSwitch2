"""Stage/sample tilt measurement during ordinary free-running tiling.

Recording is deliberately observational: no extra settling, no Z commands and
no automatic focus decisions. A DAC ``get_abs`` value is a last *commanded*
position, not independent physical position feedback.
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import datetime, timezone

from imswitch.imcommon.model import dirtools
from pathlib import Path

import numpy as np


def tilt_calibrations_root() -> Path:
    """Reusable calibrations live in the ImSwitch workspace, not tile datasets."""
    return Path(dirtools.UserFileDirs.Root) / 'imcontrol' / 'tilt_calibrations'


def new_tilt_calibration_folder(name: str = '') -> Path:
    """Create a discoverable path without relying on the editable display name.

    Keep the timestamp first for chronological file browsing. The name is a
    snapshot at recording time: renaming metadata later does not move files or
    invalidate saved calibration paths. Replace Windows-invalid path characters
    (also on non-Windows development hosts).
    """
    if not isinstance(name, str):
        raise TypeError('Calibration name must be text')
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name.strip())
    slug = re.sub(r'_+', '_', re.sub(r'\s+', '_', slug)).strip(' ._-')
    slug = slug[:64].rstrip(' ._-')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    suffix = f'_{slug}' if slug else ''
    return tilt_calibrations_root() / f'calibration_{timestamp}{suffix}'


FIELDS = (
    'tile_index', 'grid_x', 'grid_y', 'stage_x_um', 'stage_y_um',
    'focus_z_um', 'focus_signal_px', 'focus_setpoint_px', 'focus_error_px',
    'measurement_age_s', 'max_measurement_age_s', 'lock_state', 'positioner_available',
    'dac_voltage_v', 'dac_voltage_clamped', 'snapshot_error',
    'valid_for_fit', 'rejection_reason',
    'timestamp_utc', 'pass_index',
)


def _finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return False
    return math.isfinite(number)


def validate_sample(sample: dict) -> str:
    """Why a sample must be left out of the fit (empty means usable).

    We intentionally do not infer whether the PI has *settled* from a single
    measurement. Focus error is recorded for later inspection, but the user-
    configured tile settle time remains the only settling policy.
    """
    if sample.get('snapshot_error'):
        return 'snapshot_unavailable'
    if sample.get('lock_state') != 'locked':
        return 'focus_not_locked'
    if sample.get('positioner_available') is False:
        return 'positioner_unavailable'
    if not all(_finite(sample.get(name)) for name in (
        'stage_x_um', 'stage_y_um', 'focus_z_um',
        'focus_signal_px', 'focus_setpoint_px', 'focus_error_px',
        'measurement_age_s',
    )):
        return 'missing_or_nonfinite_measurement'
    if float(sample['measurement_age_s']) > float(sample.get('max_measurement_age_s', 1.0)):
        return 'stale_focus_measurement'
    if sample.get('dac_voltage_clamped') is True:
        return 'dac_voltage_clamped'
    return ''


def fit_plane(samples):
    """Fit z = z0 + sx*(x-x0) + sy*(y-y0); return JSON-safe metrics.

    Uses actual stage-axis coordinates (not camera/image orientation). Requires
    noncollinear XY coverage; does not extrapolate or reinterpret drift as tilt.
    """
    valid = [sample for sample in samples if sample.get('valid_for_fit')]
    if len(valid) < 3:
        return None
    values = np.array([[float(s['stage_x_um']), float(s['stage_y_um']),
                        float(s['focus_z_um'])] for s in valid], dtype=float)
    if not np.isfinite(values).all():
        return None
    x, y, z = values.T
    x0, y0 = float(x.mean()), float(y.mean())
    A = np.column_stack((np.ones(len(x)), x - x0, y - y0))
    if np.linalg.matrix_rank(A) < 3:
        return None
    coefficients, _, _, _ = np.linalg.lstsq(A, z, rcond=None)
    fitted = A @ coefficients
    residual = z - fitted
    return {
        'model': 'plane',
        'equation': 'z = z0 + slope_x*(x-x0) + slope_y*(y-y0)',
        'reference_xy_um': [x0, y0],
        'z0_um': float(coefficients[0]),
        'slope_x_um_per_um': float(coefficients[1]),
        'slope_y_um_per_um': float(coefficients[2]),
        'rms_residual_um': float(np.sqrt(np.mean(residual ** 2))),
        'max_abs_residual_um': float(np.max(np.abs(residual))),
        'measured_z_range_um': float(np.ptp(z)),
        'predicted_z_range_um': float(np.ptp(fitted)),
        'n_fit_points': len(valid),
        'valid_xy_bounds_um': {'x': [float(x.min()), float(x.max())],
                               'y': [float(y.min()), float(y.max())]},
    }


def paired_fit_samples(samples):
    """Pair same-XY forward/reverse readings, keeping only valid pairs.

    The fit gives each spatial location exactly one vote, so the reverse pass
    does not double its weight or silently use an unpaired noisy measurement.
    """
    forward = {(s['grid_x'], s['grid_y']): s for s in samples
               if s.get('pass_index', 1) == 1}
    reverse = {(s['grid_x'], s['grid_y']): s for s in samples
               if s.get('pass_index') == 2}
    averaged, differences = [], []
    for key, first in forward.items():
        second = reverse.get(key)
        if not second or not first['valid_for_fit'] or not second['valid_for_fit']:
            continue
        avg = dict(first)
        avg['focus_z_um'] = (float(first['focus_z_um']) + float(second['focus_z_um'])) / 2
        avg['valid_for_fit'] = True
        averaged.append(avg)
        differences.append((float(first['stage_x_um']), float(first['stage_y_um']),
                            float(second['focus_z_um']) - float(first['focus_z_um'])))
    return averaged, differences


def save_report(samples, model, path: Path, *, pass_differences=()) -> None:
    """Render three compact calibration checks without using the Qt backend."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    valid = [s for s in samples if s.get('valid_for_fit')]
    xyz = np.array([[float(s['stage_x_um']), float(s['stage_y_um']),
                     float(s['focus_z_um'])] for s in valid])
    x, y, z = xyz.T
    x0, y0 = model['reference_xy_um']
    predicted = (model['z0_um'] + model['slope_x_um_per_um'] * (x - x0)
                 + model['slope_y_um_per_um'] * (y - y0))
    resid = z - predicted
    paired = bool(pass_differences)
    fig = Figure(figsize=(17 if paired else 14, 4.8), constrained_layout=True)
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 4 if paired else 3)
    ax1, ax2, ax3 = axes[:3]
    sc1 = ax1.scatter(x - x0, y - y0, c=z, s=50, cmap='viridis')
    fig.colorbar(sc1, ax=ax1, label='Reported focus Z (µm)')
    ax1.set(title='Measured Z map', xlabel='X relative to center (µm)',
            ylabel='Y relative to center (µm)', aspect='equal')
    ax2.scatter(predicted, z, s=40)
    extent = [min(z.min(), predicted.min()), max(z.max(), predicted.max())]
    ax2.plot(extent, extent, linestyle='--', color='gray')
    ax2.set(title='Measured vs fitted Z', xlabel='Fitted Z (µm)',
            ylabel='Reported focus Z (µm)')
    sc3 = ax3.scatter(x - x0, y - y0, c=resid, s=50, cmap='coolwarm',
                      vmin=-max(abs(resid).max(), 1e-9),
                      vmax=max(abs(resid).max(), 1e-9))
    fig.colorbar(sc3, ax=ax3, label='Residual (µm)')
    ax3.set(title=f'Residual map (RMS {model["rms_residual_um"]:.3g} µm)',
            xlabel='X relative to center (µm)',
            ylabel='Y relative to center (µm)', aspect='equal')
    if paired:
        ax4 = axes[3]
        values = np.asarray(pass_differences, dtype=float)
        scale = max(float(np.max(np.abs(values[:, 2]))), 1e-9)
        sc4 = ax4.scatter(values[:, 0] - x0, values[:, 1] - y0,
                          c=values[:, 2], s=35, cmap='coolwarm',
                          vmin=-scale, vmax=scale)
        fig.colorbar(sc4, ax=ax4, label='Reverse − forward Z (µm)')
        ax4.set(title='Pass difference', xlabel='X relative to center (µm)',
                ylabel='Y relative to center (µm)', aspect='equal')
    fig.suptitle(f'Stage tilt calibration · {len(valid)} valid fit positions')
    fig.savefig(str(path), dpi=140)
    fig.clear()


class StageTiltRecorder:
    """Append each settled tile immediately; finalize the plane on any exit."""

    def __init__(self, folder: Path, *, step_um: float, settle_s: float,
                 focus_positioner: str, xy_positioner: str, n_tiles: int,
                 setup_file_name: str = "", bidirectional: bool = False,
                 calibration_only: bool = False, sampling_grid_xy=None,
                 area_span_um=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file = (self.folder / 'tilt_measurements.csv').open(
            'w', newline='', encoding='utf-8'
        )
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        self._writer.writeheader()
        self._file.flush()
        self.samples = []
        self._origin_initial = None
        self._origin_final = None
        self._bidirectional = bool(bidirectional)
        self.metadata = {
            'version': 1,
            'focus_positioner': focus_positioner,
            'setup_file_name': setup_file_name,
            'xy_positioner': xy_positioner,
            'n_requested_tiles': n_tiles,
            'tile_step_um': float(step_um),
            'settle_time_s': float(settle_s),
            'z_source': 'positioner_get_abs_software_command_or_physical_readback',
            'bidirectional': self._bidirectional,
            'calibration_only': bool(calibration_only),
            'sampling_grid_xy': list(sampling_grid_xy) if sampling_grid_xy else None,
            'area_span_um': list(area_span_um) if area_span_um else None,
            'xy_source': 'initial_stage_position_plus_commanded_tile_offsets',
            'created_utc': datetime.now(timezone.utc).isoformat(),
        }

    def record(self, *, tile_index: int, gx: int, gy: int, x_um: float,
               y_um: float, focus: dict, pass_index: int = 1) -> dict:
        sample = {
            'tile_index': int(tile_index), 'grid_x': int(gx), 'grid_y': int(gy),
            'stage_x_um': float(x_um), 'stage_y_um': float(y_um),
            **focus,
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'pass_index': int(pass_index),
        }
        reason = validate_sample(sample)
        sample['valid_for_fit'] = not bool(reason)
        sample['rejection_reason'] = reason
        self.samples.append(sample)
        self._writer.writerow({name: sample.get(name) for name in FIELDS})
        self._file.flush()  # interrupted tiling still leaves a usable trace
        return sample

    def record_origin(self, *, focus: dict, x_um: float, y_um: float,
                      final: bool = False) -> None:
        """Store origin diagnostics separately: they never affect fit weights."""
        sample = {
            'stage_x_um': float(x_um), 'stage_y_um': float(y_um),
            **focus, 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        }
        reason = validate_sample(sample)
        value = {
            'valid': not bool(reason), 'reason': reason or '',
            'z_um': float(sample['focus_z_um']) if not reason else None,
            'timestamp_utc': sample['timestamp_utc'],
        }
        if final:
            self._origin_final = value
        else:
            self._origin_initial = value

    def finish(self, *, completed: bool) -> dict:
        if not self._file.closed:
            self._file.close()
        if self._bidirectional:
            fit_samples, differences = paired_fit_samples(self.samples)
        else:
            fit_samples, differences = self.samples, []
        model = fit_plane(fit_samples)
        rejected = [s for s in self.samples if not s['valid_for_fit']]
        drift = {
            'available': False,
            'initial_z_um': self._origin_initial['z_um'] if self._origin_initial else None,
            'final_z_um': self._origin_final['z_um'] if self._origin_final else None,
            'delta_z_um': None,
            'duration_s': None,
            'initial': self._origin_initial,
            'final': self._origin_final,
        }
        if self._origin_initial and self._origin_final:
            if self._origin_initial['valid'] and self._origin_final['valid']:
                drift['available'] = True
                drift['delta_z_um'] = (self._origin_final['z_um']
                                      - self._origin_initial['z_um'])
                drift['duration_s'] = (
                    datetime.fromisoformat(self._origin_final['timestamp_utc'])
                    - datetime.fromisoformat(self._origin_initial['timestamp_utc'])
                ).total_seconds()
        pair_diagnostics = None
        if self._bidirectional:
            deltas = np.asarray([entry[2] for entry in differences], dtype=float)
            pair_diagnostics = {
                'n_valid_pairs': len(differences),
                'n_positions_without_valid_pair': (
                    self.metadata['n_requested_tiles'] - len(differences)
                ),
                'mean_reverse_minus_forward_um': (float(deltas.mean()) if len(deltas) else None),
                'rms_reverse_minus_forward_um': (
                    float(np.sqrt(np.mean(deltas**2))) if len(deltas) else None
                ),
                'max_abs_reverse_minus_forward_um': (
                    float(np.max(np.abs(deltas))) if len(deltas) else None
                ),
            }
        result = {
            **self.metadata,
            'completed': bool(completed),
            'n_recorded_tiles': len(self.samples),
            'n_valid_tiles': len(self.samples) - len(rejected),
            'rejected_tiles': [
                ({'tile_index': s['tile_index'], 'pass_index': s.get('pass_index', 1),
                  'reason': s['rejection_reason']} if self._bidirectional else
                 {'tile_index': s['tile_index'], 'reason': s['rejection_reason']})
                for s in rejected
            ],
            'origin_drift': drift,
            'bidirectional_diagnostics': pair_diagnostics,
            'fit': model,
            'fit_status': 'ok' if model is not None else 'insufficient_noncollinear_valid_points',
        }
        path = self.folder / 'tilt_fit.json'
        path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n',
                        encoding='utf-8')
        if model is not None:
            try:
                save_report(fit_samples, model, self.folder / 'tilt_fit.png',
                            pass_differences=differences)
            except (ImportError, OSError, ValueError) as error:
                result['plot_error'] = f'{type(error).__name__}: {error}'
                path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n',
                                encoding='utf-8')
        return result

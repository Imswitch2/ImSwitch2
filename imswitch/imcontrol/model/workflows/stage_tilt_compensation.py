"""Relative XY-to-Z tilt feed-forward for free-running tiling.

The calibration's z0 is deliberately not a positioner target. The user first
focuses at the run origin; only the *difference* predicted by the two slopes is
commanded. This module has no Qt/hardware dependencies.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


class TiltSafetyError(ValueError):
    """A tilt calibration or a planned compensation exceeds a safety limit."""


@dataclass(frozen=True)
class TiltSafetyLimits:
    """Limits that always apply to Apply (not only with Pre-check).

    Defaults are deliberately small but cover the tilts seen so far (about
    9 µm/mm on one rig): 15 µm/mm per axis, 30 µm in total per run and 5 µm
    per move leave room for real samples while refusing a wrong or foreign
    calibration before anything moves.
    """
    max_abs_slope_um_per_mm: float = 15.0   # per axis
    max_excursion_um: float = 30.0          # |Z - Z_start| over the run
    max_step_um: float = 5.0                # |ΔZ| per move
    min_fit_points: int = 4                 # more than three
    max_rms_um: float = 1.0                 # fit RMS residual
    max_abs_residual_um: float = 3.0        # worst fit residual
    max_age_days: float = 30.0              # older needs confirmation

    def validate(self) -> 'TiltSafetyLimits':
        for name in ('max_abs_slope_um_per_mm', 'max_excursion_um', 'max_step_um',
                     'max_rms_um', 'max_abs_residual_um', 'max_age_days'):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f'Tilt limit {name} must be a positive number')
        if int(self.min_fit_points) < 4:
            raise ValueError('A tilt fit needs more than three points')
        if self.max_step_um > self.max_excursion_um:
            raise ValueError('Tilt max step must not exceed the max excursion')
        return self


@dataclass(frozen=True)
class CompensationPlan:
    """Predicted Z offsets (from the run's Z start) for one grid."""
    deltas_um: tuple                 # per tile, in acquisition order
    max_excursion_um: float
    max_step_um: float

    def describe(self) -> str:
        return (f'max |ΔZ| {self.max_excursion_um:.2f} µm, '
                f'max step {self.max_step_um:.2f} µm')


@dataclass(frozen=True)
class TiltPlane:
    path: Path
    sha256: str
    xy_positioner: str
    focus_positioner: str
    slope_x: float
    slope_y: float
    x_bounds: tuple[float, float]
    y_bounds: tuple[float, float]
    rms_residual_um: float
    max_abs_residual_um: float = float('nan')
    n_fit_points: int = 0
    created_utc: str = ''
    setup_file_name: str = ''
    #: Calibrated span relative to the calibration's own origin, when stored.
    relative_x_bounds: tuple | None = None
    relative_y_bounds: tuple | None = None
    n_outliers_rejected: int = 0

    def age_days(self, now: datetime | None = None) -> float | None:
        try:
            created = datetime.fromisoformat(self.created_utc)
        except (TypeError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - created).total_seconds() / 86400.0

    def summary(self) -> str:
        date = self.created_utc[:10] if self.created_utc else 'date unknown'
        return (f'{date} · RMS {self.rms_residual_um:.3g} µm · '
                f'slopes X {self.slope_x * 1000:+.2f} / Y {self.slope_y * 1000:+.2f} µm/mm'
                + (f' · {self.n_outliers_rejected} outlier(s) rejected'
                   if self.n_outliers_rejected else ''))

    def foreign_or_stale(self, *, setup_name: str, limits: 'TiltSafetyLimits',
                         now: datetime | None = None) -> list:
        """Reasons this calibration needs an explicit confirmation."""
        reasons = []
        if self.setup_file_name and setup_name and self.setup_file_name != setup_name:
            reasons.append(f'recorded on setup {self.setup_file_name!r}, '
                           f'this is {setup_name!r}')
        if not self.setup_file_name:
            reasons.append('the calibration does not name its setup')
        age = self.age_days(now)
        if age is None:
            reasons.append('the calibration has no valid date')
        elif age > limits.max_age_days:
            reasons.append(f'{age:.0f} days old (limit {limits.max_age_days:g})')
        return reasons

    def delta_z(self, dx_um: float, dy_um: float) -> float:
        delta = self.slope_x * float(dx_um) + self.slope_y * float(dy_um)
        if not math.isfinite(delta):
            raise ValueError('Nonfinite tilt Z correction')
        return delta

    def check_xy(self, x_um: float, y_um: float) -> None:
        """Do not extrapolate beyond the calibrated area (small float tolerance)."""
        x_um, y_um = float(x_um), float(y_um)
        if not (math.isfinite(x_um) and math.isfinite(y_um)):
            raise ValueError('Nonfinite tiling XY position')
        tolerance_um = 1.0  # allow small stage re-positioning differences, not a new FOV
        if not (self.x_bounds[0] - tolerance_um <= x_um <= self.x_bounds[1] + tolerance_um
                and self.y_bounds[0] - tolerance_um <= y_um <= self.y_bounds[1] + tolerance_um):
            raise ValueError(
                f'Tile XY ({x_um:.3f}, {y_um:.3f}) µm is outside the calibrated '
                f'area X={self.x_bounds}, Y={self.y_bounds} µm'
            )

    def check_grid(self, *, origin_xy, moves, step_um: float) -> None:
        gx = gy = 0
        for dx, dy in moves:
            gx += dx
            gy += dy
            self.check_xy(origin_xy[0] + gx * step_um,
                          origin_xy[1] + gy * step_um)

    def check_span(self, *, moves, step_um: float) -> None:
        """The run's XY span must fit inside the calibrated span (relative
        to each origin) -- holds even when the stage's absolute frame was
        re-zeroed since the calibration."""
        gx = gy = 0
        xs, ys = [0.0], [0.0]
        for dx, dy in moves:
            gx += dx
            gy += dy
            xs.append(gx * step_um)
            ys.append(gy * step_um)
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        cal_x = self.x_bounds[1] - self.x_bounds[0]
        cal_y = self.y_bounds[1] - self.y_bounds[0]
        if span_x > cal_x + 1.0 or span_y > cal_y + 1.0:
            raise TiltSafetyError(
                f'The tiling area ({span_x:.0f} × {span_y:.0f} µm) is larger than the '
                f'calibrated area ({cal_x:.0f} × {cal_y:.0f} µm); refusing to extrapolate')


def plan_compensation(plane: TiltPlane, *, moves, step_um: float,
                      limits: TiltSafetyLimits, origin_xy=None) -> CompensationPlan:
    """Predict every commanded Z offset and refuse anything over a limit.

    Called before any movement (and for the panel's preview without an
    origin). With ``origin_xy`` the XY bounds/extrapolation check is
    included; it is mandatory at run start.
    """
    limits.validate()
    for label, slope in (('X', plane.slope_x), ('Y', plane.slope_y)):
        if abs(slope) * 1000.0 > limits.max_abs_slope_um_per_mm:
            raise TiltSafetyError(
                f'Calibration {label} slope {slope * 1000:+.2f} µm/mm exceeds the '
                f'{limits.max_abs_slope_um_per_mm:g} µm/mm limit')
    moves = list(moves)
    plane.check_span(moves=moves, step_um=step_um)
    if origin_xy is not None:
        plane.check_grid(origin_xy=origin_xy, moves=moves, step_um=step_um)
    gx = gy = 0
    deltas, previous, max_step = [], 0.0, 0.0
    for dx, dy in moves:
        gx += dx
        gy += dy
        delta = plane.delta_z(gx * step_um, gy * step_um)
        max_step = max(max_step, abs(delta - previous))
        previous = delta
        deltas.append(delta)
    excursion = max((abs(d) for d in deltas), default=0.0)
    if excursion > limits.max_excursion_um:
        raise TiltSafetyError(
            f'Tilt compensation would move Z by up to {excursion:.2f} µm from the start; '
            f'limit {limits.max_excursion_um:g} µm')
    if max_step > limits.max_step_um:
        raise TiltSafetyError(
            f'Tilt compensation would step Z by {max_step:.2f} µm in one move; '
            f'limit {limits.max_step_um:g} µm')
    return CompensationPlan(tuple(deltas), excursion, max_step)


def return_path(plane: TiltPlane, offset_xy_um, *, step_um: float,
                max_z_step_um: float):
    """Segments ``(dx, dy, delta_z_at_end)`` taking XY from ``offset_xy_um``
    (relative to the run origin) back to 0 in a straight line, with the Z
    offset following the plane: Z tracks the sample surface the whole way
    instead of being restored before or after the XY travel. No segment is
    longer than ``step_um`` or changes Z by more than ``max_z_step_um``."""
    ox, oy = float(offset_xy_um[0]), float(offset_xy_um[1])
    length = math.hypot(ox, oy)
    if length == 0:
        return []
    dz_total = abs(plane.delta_z(ox, oy))
    n = max(1, math.ceil(length / max(step_um, 1e-9)),
            math.ceil(dz_total / max(max_z_step_um, 1e-9)))
    segments, px, py = [], ox, oy
    for k in range(1, n + 1):
        f = 1.0 - k / n
        nx, ny = ox * f, oy * f
        segments.append((nx - px, ny - py, plane.delta_z(nx, ny) if k < n else 0.0))
        px, py = nx, ny
    return segments


def _finite(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'Invalid {label}: {value!r}') from exc
    if not math.isfinite(number):
        raise ValueError(f'Nonfinite {label}: {value!r}')
    return number


def load_tilt_plane(path, *, xy_positioner: str, focus_positioner: str | None = None,
                    limits: TiltSafetyLimits | None = None) -> TiltPlane:
    """Validate only the completed v1 calibration format produced by ImSwitch.

    The fit quality gate applies at load: more than three points, RMS and
    worst residual within ``limits``. A file that lacks what the gate needs
    is refused with a request to re-record."""
    limits = (limits or TiltSafetyLimits()).validate()
    path = Path(path).expanduser().resolve()
    data_bytes = path.read_bytes()
    document = json.loads(data_bytes)
    if not isinstance(document, dict) or document.get('version') != 1:
        raise ValueError('Expected a version 1 tilt_fit.json file')
    if document.get('completed') is not True or document.get('fit_status') != 'ok':
        raise ValueError('Tilt calibration must be a complete, successful fit')
    if document.get('xy_positioner') != xy_positioner:
        raise ValueError('Calibration XY positioner does not match the active setup')
    calibrated_z = document.get('focus_positioner')
    if not isinstance(calibrated_z, str) or not calibrated_z:
        raise ValueError('Calibration has no Z positioner identity')
    if focus_positioner is not None and calibrated_z != focus_positioner:
        raise ValueError('Calibration Z positioner does not match the requested actuator')
    # Recordings may contain either software-tracked Z (DAC/NI-DAQ) or
    # physical readback (e.g. Leica). Both have µm units and the same XY→Z
    # plane contract. Keep the original command-only v1 format readable.
    if document.get('z_source') not in (
        'last_commanded_position_not_hardware_readback',
        'positioner_get_abs_software_command_or_physical_readback',
    ):
        raise ValueError('Unknown calibration Z units/source')
    fit = document.get('fit')
    if not isinstance(fit, dict) or fit.get('model') != 'plane':
        raise ValueError('Calibration contains no planar fit')
    n_points = int(fit.get('n_fit_points', 0))
    if n_points < limits.min_fit_points:
        raise TiltSafetyError(
            f'Calibration has {n_points} fitted points; at least {limits.min_fit_points} '
            'are needed. Re-record it with more sampling positions.')
    if 'max_abs_residual_um' not in fit:
        raise TiltSafetyError('Calibration lacks the residuals the quality gate needs; '
                              're-record it.')
    rms = _finite(fit.get('rms_residual_um'), 'fit RMS')
    worst = _finite(fit.get('max_abs_residual_um'), 'fit max residual')
    if rms > limits.max_rms_um:
        raise TiltSafetyError(f'Calibration fit RMS {rms:.3g} µm exceeds the '
                              f'{limits.max_rms_um:g} µm gate; re-record it.')
    if worst > limits.max_abs_residual_um:
        raise TiltSafetyError(f'Calibration worst residual {worst:.3g} µm exceeds the '
                              f'{limits.max_abs_residual_um:g} µm gate; re-record it.')
    gate = fit.get('quality_gate')
    if isinstance(gate, dict) and gate.get('passed') is False:
        raise TiltSafetyError('Calibration failed its quality gate when recorded: '
                              + '; '.join(map(str, gate.get('reasons', []))))
    bounds = fit.get('valid_xy_bounds_um')
    if not isinstance(bounds, dict):
        raise ValueError('Calibration has no XY bounds')

    def _bounds(axis):
        values = bounds.get(axis)
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            raise ValueError(f'Calibration has invalid {axis} bounds')
        lo, hi = (_finite(values[0], axis), _finite(values[1], axis))
        if lo >= hi:
            raise ValueError(f'Calibration has degenerate {axis} bounds')
        return lo, hi

    relative = fit.get('valid_xy_bounds_relative_um')
    rel_x = rel_y = None
    if isinstance(relative, dict):
        try:
            rel_x = tuple(_finite(v, 'relative x') for v in relative['x'])
            rel_y = tuple(_finite(v, 'relative y') for v in relative['y'])
        except (KeyError, TypeError, ValueError):
            rel_x = rel_y = None
    return TiltPlane(
        path=path, sha256=hashlib.sha256(data_bytes).hexdigest(),
        xy_positioner=xy_positioner, focus_positioner=calibrated_z,
        slope_x=_finite(fit.get('slope_x_um_per_um'), 'X slope'),
        slope_y=_finite(fit.get('slope_y_um_per_um'), 'Y slope'),
        x_bounds=_bounds('x'), y_bounds=_bounds('y'),
        rms_residual_um=rms, max_abs_residual_um=worst, n_fit_points=n_points,
        created_utc=str(document.get('created_utc') or ''),
        setup_file_name=str(document.get('setup_file_name') or ''),
        relative_x_bounds=rel_x, relative_y_bounds=rel_y,
        n_outliers_rejected=int(fit.get('n_outliers_rejected', 0) or 0),
    )


def require_reference_signal(focus: dict, *, allow_locked=False) -> float:
    """Return a fresh optical reference when the focus camera is available."""
    if focus.get('lock_state') != 'unlocked' and not (
            allow_locked and focus.get('lock_state') == 'locked'):
        raise RuntimeError('Focus lock must be unlocked or (when allowed) locked for validation')
    if focus.get('positioner_available') is False:
        raise RuntimeError('Compensation Z positioner is unavailable')
    signal = _finite(focus.get('focus_signal_px'), 'first-tile focus signal')
    age = _finite(focus.get('measurement_age_s'), 'first-tile focus signal age')
    max_age = _finite(focus.get('max_measurement_age_s'), 'maximum focus signal age')
    if age > max_age:
        raise RuntimeError('First-tile focus-camera estimate is stale')
    return signal


def validate_z_target(positioner, target_um: float, *, strict_limits=False) -> None:
    """Reject invalid numbers, unavailable or unreferenced devices, and
    targets outside the output's voltage range -- always. (``strict_limits``
    is kept for callers; the checks no longer depend on it.)"""
    target_um = _finite(target_um, 'Z target')
    if getattr(positioner, 'isAvailable', True) is False:
        raise RuntimeError('Compensation Z positioner is unavailable')
    # Always, not only with Pre-check: an unreferenced Z has no meaningful
    # absolute frame for the plane's targets.
    if getattr(positioner, 'requiresReference', False) and not getattr(positioner, 'isReferenced', False):
        raise RuntimeError('Compensation Z positioner must be referenced before Apply')
    fields = ('_offset_voltage', '_volts_per_um', '_min_voltage', '_max_voltage')
    if all(hasattr(positioner, field) for field in fields):
        voltage = (_finite(positioner._offset_voltage, 'DAC offset')
                   + target_um * _finite(positioner._volts_per_um, 'DAC scale'))
        low = _finite(positioner._min_voltage, 'DAC minimum voltage')
        high = _finite(positioner._max_voltage, 'DAC maximum voltage')
        if not math.isfinite(voltage) or not low <= voltage <= high:
            raise ValueError(
                f'Tilt correction would request DAC voltage {voltage:g} V '
                f'outside [{low:g}, {high:g}] V; refusing to clamp'
            )
    elif all(hasattr(positioner, name) for name in ('_conversionFactor', '_minVolt', '_maxVolt')):
        conversion = _finite(positioner._conversionFactor, 'NI-DAQ conversion factor')
        if conversion == 0:
            raise ValueError('NI-DAQ Z conversion factor must be nonzero')
        voltage = target_um / conversion
        lo = _finite(positioner._minVolt, 'NI-DAQ minVolt')
        hi = _finite(positioner._maxVolt, 'NI-DAQ maxVolt')
        if not lo <= voltage <= hi:
            raise ValueError(
                f'Tilt correction would request NI-DAQ voltage {voltage:g} V '
                f'outside [{lo:g}, {hi:g}] V'
            )


_FIELDS = (
    'tile_index', 'grid_x', 'grid_y', 'stage_x_um', 'stage_y_um',
    'predicted_delta_z_um', 'predicted_z_um', 'commanded_z_um',
    'focus_signal_px', 'focus_signal_delta_px', 'measurement_age_s',
    'lock_state', 'valid_focus_signal', 'rejection_reason', 'timestamp_utc',
)


class TiltCompensationRecorder:
    """Append settled tiles and their focus-camera estimate for offline inspection."""

    def __init__(self, folder: Path, *, plane: TiltPlane, origin_xy, origin_z_um,
                 origin_signal_px, n_tiles: int):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file = (self.folder / 'tilt_compensation.csv').open(
            'w', newline='', encoding='utf-8'
        )
        self._writer = csv.DictWriter(self._file, fieldnames=_FIELDS)
        self._writer.writeheader()
        self._file.flush()
        self.samples = []
        self._plane = plane
        self.metadata = {
            'version': 1,
            'calibration_path': str(plane.path),
            'calibration_sha256': plane.sha256,
            'xy_positioner': plane.xy_positioner,
            'focus_positioner': plane.focus_positioner,
            'slope_x_um_per_um': plane.slope_x,
            'slope_y_um_per_um': plane.slope_y,
            'origin_xy_um': [float(v) for v in origin_xy],
            'origin_commanded_z_um': _finite(origin_z_um, 'initial Z'),
            'reference_focus_signal_px': (_finite(origin_signal_px, 'reference focus signal')
                                          if origin_signal_px is not None else None),
            'n_requested_tiles': int(n_tiles),
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'z_source': 'absolute_command_target_not_arrival_verified',
        }

    def record(self, *, tile_index, gx, gy, x_um, y_um, commanded_z_um,
               focus: dict) -> dict:
        signal, age = focus.get('focus_signal_px'), focus.get('measurement_age_s')
        reason = ''
        if focus.get('lock_state') not in ('unlocked', 'locked'):
            reason = 'focus_not_observable'
        elif focus.get('positioner_available') is False:
            reason = 'positioner_unavailable'
        else:
            try:
                signal = _finite(signal, 'focus signal')
                age = _finite(age, 'focus signal age')
                if age > _finite(focus.get('max_measurement_age_s'), 'maximum signal age'):
                    reason = 'stale_focus_signal'
            except ValueError:
                reason = 'missing_or_nonfinite_focus_signal'
        ox, oy = self.metadata['origin_xy_um']
        predicted_delta = self._plane_delta(x_um - ox, y_um - oy)
        record = dict(
            tile_index=int(tile_index), grid_x=int(gx), grid_y=int(gy),
            stage_x_um=float(x_um), stage_y_um=float(y_um),
            predicted_delta_z_um=predicted_delta,
            predicted_z_um=self.metadata['origin_commanded_z_um'] + predicted_delta,
            commanded_z_um=_finite(commanded_z_um, 'commanded Z'),
            focus_signal_px=signal if reason == '' else None,
            focus_signal_delta_px=(signal - self.metadata['reference_focus_signal_px']
                                   if reason == '' and self.metadata['reference_focus_signal_px'] is not None
                                   else None),
            measurement_age_s=age,
            lock_state=focus.get('lock_state'),
            valid_focus_signal=not bool(reason), rejection_reason=reason,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
        self.samples.append(record)
        self._writer.writerow(record)
        self._file.flush()
        return record

    def _plane_delta(self, dx, dy):
        return self._plane.delta_z(dx, dy)

    def finish(self, *, completed: bool, failure: str = '') -> dict:
        if not self._file.closed:
            self._file.close()
        accepted = [s for s in self.samples if s['valid_focus_signal']
                    and s['focus_signal_delta_px'] is not None]
        errors = [float(s['focus_signal_delta_px']) for s in accepted]
        result = {
            **self.metadata,
            'completed': bool(completed),
            'failure': str(failure),
            'n_recorded_tiles': len(self.samples),
            'n_valid_focus_samples': len(accepted),
            'max_abs_focus_signal_delta_px': max(map(abs, errors), default=None),
            'rms_focus_signal_delta_px': (float(np.sqrt(np.mean(np.square(errors))))
                                          if errors else None),
        }
        out = self.folder / 'tilt_validation.json'
        out.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        if accepted:
            try:
                _save_validation_plot(self.samples, self.folder / 'tilt_validation.png')
            except (ImportError, OSError, ValueError) as exc:
                result['plot_error'] = f'{type(exc).__name__}: {exc}'
                out.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        return result


def _save_validation_plot(samples, path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    accepted = [s for s in samples if s['valid_focus_signal']
                and s['focus_signal_delta_px'] is not None]
    x = np.array([s['stage_x_um'] for s in accepted])
    y = np.array([s['stage_y_um'] for s in accepted])
    error = np.array([s['focus_signal_delta_px'] for s in accepted])
    index = np.array([s['tile_index'] for s in accepted])
    center_x, center_y = np.mean(x), np.mean(y)
    fig = Figure(figsize=(11, 4.5), constrained_layout=True)
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    scale = max(float(np.max(np.abs(error))), 1e-9)
    image = axes[0].scatter(x - center_x, y - center_y, c=error, cmap='coolwarm',
                            vmin=-scale, vmax=scale, s=65)
    fig.colorbar(image, ax=axes[0], label='Focus signal shift (px)')
    axes[0].set(xlabel='X relative to center (µm)',
                ylabel='Y relative to center (µm)', title='Focus signal relative to first tile', aspect='equal')
    axes[1].plot(index, error, 'o-')
    axes[1].axhline(0, color='gray', linestyle='--')
    axes[1].set(xlabel='Tile acquisition order', ylabel='Focus signal shift (px)',
                title='Residual focus signal by tile')
    fig.suptitle(f'Tilt compensation validation · {len(accepted)}/{len(samples)} valid focus samples')
    fig.savefig(str(path), dpi=140)
    fig.clear()

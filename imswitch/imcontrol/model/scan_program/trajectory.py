"""Free trajectories and the gridded image (docs/design/plans/
scan-engines-and-expert-scan.md §1.5, §6, M4).

A trajectory program moves its devices along a path -- a circle, a spiral, a
Lissajous figure, a list of points, a few lines of Python -- and point
detectors measure window after window along it. Each window is one
measurement over its **realized** integration time, placed at the commanded
position of its centre (the stated assumption: a linear detector, no
dead-time correction, the path followed as commanded).

The windows are binned onto a grid that covers them, at the trajectory's
pixel size, by what the detector measures:

- a photon counter (APD): Σ counts / Σ seconds -- a count rate, ``counts/s``;
- an analog detector (PMT): Σ (volts · seconds) / Σ seconds -- the
  integration-time-weighted mean, ``V``.

Weighting by integration time keeps the value right when windows differ in
length (the last one usually does). A bin no window reached is NaN; a
measured zero is 0. The coverage -- each bin's total integration time, in
seconds -- is the auxiliary measurement that tells sparse from dark.

Qt-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

import numpy as np

from .policy import resolve_policy
from .program import PATH_ANCHOR, ProgramError, ScanProgram
from .schedule import ScheduleRefused, realized_duration

COUNTS = 'counts'
MEAN = 'mean'

#: vel_max and acc_max are declared per microsecond (µm/µs, µm/µs²).
US_PER_S = 1e6

#: What each measurement grids to, and its unit (plan §6).
MEASUREMENTS = {
    COUNTS: ('Σ counts / Σ integration time', 'counts/s'),
    MEAN: ('Σ (value · integration time) / Σ integration time', 'V'),
}

#: The assumptions the gridded image rests on, recorded with it (plan §6).
ASSUMPTIONS = ('a linear detector', 'no dead-time correction',
               'each window at the commanded position of its centre')


# ---------------------------------------------------------------------------
# Patterns: a path as a function of time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pattern:
    """A path pattern: ``path(params, t, duration)`` gives, for times ``t``
    (s, from the start of the path), one row of µm per device. ``source``
    says where it came from: built in, or a drop-in file and its SHA-256
    (``pattern_plugins``)."""
    id: str
    title: str
    dimensions: int
    parameters: Tuple[Tuple[str, str, str], ...]     # (name, label, kind)
    path: Callable[[Mapping, np.ndarray, float], np.ndarray]
    source: str = 'built-in'


def _pair(params, name):
    value = params[name]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ProgramError(f'{name} is a pair (x, y), not {value!r}.')
    return float(value[0]), float(value[1])


def _circle(params, t, duration):
    cx, cy = _pair(params, 'center_um')
    radius, turns = float(params['radius_um']), float(params['turns'])
    angle = 2 * np.pi * turns * t / duration
    return np.vstack([cx + radius * np.cos(angle), cy + radius * np.sin(angle)])


def _spiral(params, t, duration):
    """An Archimedean spiral from the centre outwards. ``speed`` 'angular'
    turns at a constant rate (the path speeds up outwards); 'linear' keeps
    the path speed nearly constant (r and the angle grow with sqrt(t))."""
    cx, cy = _pair(params, 'center_um')
    radius, turns = float(params['radius_um']), float(params['turns'])
    fraction = t / duration
    if params.get('speed', 'angular') == 'linear':
        fraction = np.sqrt(fraction)
    elif params.get('speed', 'angular') != 'angular':
        raise ProgramError(f"A spiral's speed is 'angular' or 'linear', not {params['speed']!r}.")
    r = radius * fraction
    angle = 2 * np.pi * turns * fraction
    return np.vstack([cx + r * np.cos(angle), cy + r * np.sin(angle)])


def _lissajous(params, t, duration):
    cx, cy = _pair(params, 'center_um')
    ax, ay = _pair(params, 'amplitude_um')
    fx, fy = _pair(params, 'frequency_hz')
    phase = float(params['phase_rad'])
    return np.vstack([cx + ax * np.sin(2 * np.pi * fx * t + phase),
                      cy + ay * np.sin(2 * np.pi * fy * t)])


def _points(params, t, duration):
    """Points visited at equal time intervals, straight between them."""
    points = np.asarray(params['points_um'], dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2:
        raise ProgramError('points_um is a list of at least two [x, y] points.')
    along = np.linspace(0.0, duration, len(points))
    return np.vstack([np.interp(t, along, points[:, 0]), np.interp(t, along, points[:, 1])])


def _script(params, t, duration):
    """``path(t, duration)`` defined by the user's code, returning (x, y) in
    µm for an array of times in s. It runs like a Python step: not sandboxed."""
    namespace = {'np': np, 'math': math}
    exec(compile(str(params['code']), '<trajectory script>', 'exec'), namespace)
    if not callable(namespace.get('path')):
        raise ProgramError('The trajectory script defines no path(t, duration).')
    xy = np.asarray(namespace['path'](t, duration), dtype=float)
    if xy.shape != (2, len(t)):
        raise ProgramError(f'path(t, duration) must return (x, y), each like t; it returned '
                           f'shape {xy.shape}.')
    return xy


PATTERNS: Dict[str, Pattern] = {
    'circle': Pattern('circle', 'Circle', 2, (
        ('center_um', 'Centre', 'pair-micrometres'), ('radius_um', 'Radius', 'micrometres'),
        ('turns', 'Turns', 'number')), _circle),
    'spiral': Pattern('spiral', 'Spiral', 2, (
        ('center_um', 'Centre', 'pair-micrometres'), ('radius_um', 'Outer radius', 'micrometres'),
        ('turns', 'Turns', 'number'), ('speed', 'Speed', 'choice:angular,linear')), _spiral),
    'lissajous': Pattern('lissajous', 'Lissajous', 2, (
        ('center_um', 'Centre', 'pair-micrometres'),
        ('amplitude_um', 'Amplitudes', 'pair-micrometres'),
        ('frequency_hz', 'Frequencies', 'pair-hertz'), ('phase_rad', 'Phase (rad)', 'number')),
        _lissajous),
    'points': Pattern('points', 'Points (a curve from a file)', 2, (
        ('points_um', 'Points [[x, y], ...]', 'points'),), _points),
    'script': Pattern('script', 'Python: path(t, duration)', 2, (
        ('code', 'Code', 'code'),), _script),
}


def path_positions(trajectory, times_s: np.ndarray) -> np.ndarray:
    """The commanded position of every device at ``times_s`` (rows: devices)."""
    pattern = PATTERNS[trajectory.pattern]
    try:
        xy = pattern.path(trajectory.params, np.asarray(times_s, dtype=float),
                          float(trajectory.duration_s))
    except KeyError as error:
        raise ProgramError(f'The {pattern.title} pattern needs {error.args[0]}.') from None
    if not np.all(np.isfinite(xy)):
        raise ProgramError(f'The {pattern.title} path is not finite everywhere.')
    return xy


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    """Bins of ``pixel_um`` centred on ``first_um + k * pixel_um`` along each
    device; ``shape`` is the number of bins per device, the first fastest."""
    devices: Tuple[str, ...]
    first_um: Tuple[float, ...]
    pixel_um: float
    shape: Tuple[int, ...]

    @classmethod
    def covering(cls, devices, centres_um: np.ndarray, pixel_um: float) -> 'Grid':
        first = centres_um.min(axis=0)
        last = centres_um.max(axis=0)
        shape = tuple(int(round((hi - lo) / pixel_um)) + 1 for lo, hi in zip(first, last))
        return cls(tuple(devices), tuple(float(v) for v in first), float(pixel_um), shape)

    @property
    def image_shape(self) -> Tuple[int, ...]:
        """The image's shape: the last device is the slowest (rows)."""
        return tuple(reversed(self.shape))

    def flat_index(self, centres_um: np.ndarray) -> np.ndarray:
        index = np.rint((centres_um - np.asarray(self.first_um)) / self.pixel_um).astype(int)
        flat = np.zeros(len(centres_um), dtype=int)
        stride = 1
        for axis, count in enumerate(self.shape):
            flat += np.clip(index[:, axis], 0, count - 1) * stride
            stride *= count
        return flat

    def axis_um(self, axis) -> np.ndarray:
        return self.first_um[axis] + self.pixel_um * np.arange(self.shape[axis])

    def as_dict(self):
        return {'devices': list(self.devices), 'first_um': list(self.first_um),
                'pixel_um': self.pixel_um, 'shape': list(self.shape)}


class GridAccumulator:
    """Windows binned onto a grid, as they arrive."""

    def __init__(self, grid: Grid, measurement: str):
        if measurement not in MEASUREMENTS:
            raise ValueError(f'A measurement is {" or ".join(MEASUREMENTS)}, not {measurement!r}.')
        self.grid, self.measurement = grid, measurement
        size = int(np.prod(grid.shape))
        self._sum = np.zeros(size)
        self._seconds = np.zeros(size)

    def add(self, flat_index, values, seconds):
        """``values``: counts per window (counts), or each window's mean
        (mean); ``seconds``: each window's realized integration time."""
        values = np.asarray(values, dtype=float)
        seconds = np.asarray(seconds, dtype=float)
        weighted = values if self.measurement == COUNTS else values * seconds
        np.add.at(self._sum, flat_index, weighted)
        np.add.at(self._seconds, flat_index, seconds)

    def image(self) -> np.ndarray:
        out = np.full(self._sum.shape, np.nan)
        hit = self._seconds > 0
        out[hit] = self._sum[hit] / self._seconds[hit]
        return out.reshape(self.grid.image_shape)

    def coverage(self) -> np.ndarray:
        return self._seconds.reshape(self.grid.image_shape).copy()

    @property
    def unit(self):
        return MEASUREMENTS[self.measurement][1]


# ---------------------------------------------------------------------------
# Realization on an engine's clock
# ---------------------------------------------------------------------------

@dataclass
class RealizedTrajectory:
    program: ScanProgram
    rate_hz: float
    policy: object
    approach_ticks: int
    path_ticks: int
    window_start: np.ndarray       # ticks from the start of the stream
    window_ticks: np.ndarray
    centres_um: np.ndarray         # (windows, devices)
    positions_um: Dict[str, np.ndarray]
    digital: Dict[str, np.ndarray]
    grid: Grid
    sources: List[Dict[str, object]] = field(default_factory=list)

    @property
    def total_ticks(self) -> int:
        # the approach, the path, and the way back, which ends on its last
        # sample exactly where the devices started
        return self.approach_ticks + self.path_ticks + self.approach_ticks + 1

    @property
    def grid_s(self) -> float:
        return 1.0 / self.rate_hz

    def window_seconds(self) -> np.ndarray:
        return self.window_ticks / self.rate_hz

    def provenance(self, measurement: str) -> Dict[str, object]:
        """What the gridded image is, recorded with it (plan §6)."""
        rule, unit = MEASUREMENTS[measurement]
        return {
            'reduction': 'gridded from a trajectory',
            'pattern': self.program.trajectory.pattern,
            'pattern_source': PATTERNS[self.program.trajectory.pattern].source,
            'grid': self.grid.as_dict(),
            'weighting': rule,
            'unit': unit,
            'coverage_unit': 's',
            'windows': int(len(self.window_ticks)),
            'assumptions': list(ASSUMPTIONS),
            'raw_measurements_stored': bool(self.program.trajectory.record_measurements),
        }


def _hermite(s, p0, v0, p1, v1, duration):
    """Positions along a cubic from (p0, v0) to (p1, v1) over ``duration``,
    at fractions ``s`` of it: position and velocity continuous at both ends."""
    h00 = 2 * s ** 3 - 3 * s ** 2 + 1
    h10 = s ** 3 - 2 * s ** 2 + s
    h01 = -2 * s ** 3 + 3 * s ** 2
    h11 = s ** 3 - s ** 2
    return h00 * p0 + h10 * duration * v0 + h01 * p1 + h11 * duration * v1


def _ticks(seconds, rate_hz, policy, quantity, where, refusals):
    realized, refusal = realized_duration(seconds, rate_hz, policy, quantity, where)
    if refusal:
        refusals.append(refusal)
    return int(round(realized * rate_hz))


def realize_trajectory(program: ScanProgram, rate_hz: float, policy=None, *,
                       captured_um: Optional[Mapping[str, float]] = None) -> RealizedTrajectory:
    """The trajectory on a ``rate_hz`` grid under ``policy`` (the program's
    own when omitted), or :class:`ScheduleRefused` with every reason."""
    trajectory = program.trajectory
    policy = resolve_policy(policy if policy is not None else program.timing_policy)
    rate = float(rate_hz)
    refusals = []
    path_ticks = _ticks(trajectory.duration_s, rate, policy, 'step', 'the path', refusals)
    window = _ticks(program.step_s, rate, policy, 'step', 'the measurement window', refusals)
    approach = _ticks(trajectory.approach_s, rate, policy, 'move', 'the approach', refusals)
    edges = []
    for event in program.timeline:
        edges.append((event, _ticks(event.start_s, rate, policy, 'event_edges',
                                    f'{event.device} start', refusals),
                      _ticks(event.end_s, rate, policy, 'event_edges',
                             f'{event.device} end', refusals)))
    if refusals:
        raise ScheduleRefused(refusals)
    if window < 1 or path_ticks < window:
        raise ScheduleRefused([f'The measurement window is {window} samples of a '
                               f'{path_ticks}-sample path.'])

    starts = np.arange(0, path_ticks, window)
    lengths = np.minimum(window, path_ticks - starts)
    centres_t = (starts + lengths / 2.0) / rate
    centres = path_positions(trajectory, centres_t).T
    samples = path_positions(trajectory, np.arange(path_ticks) / rate)

    # The approach leaves the captured position at rest and joins the path
    # with its position and velocity; the way back leaves the path's end the
    # same way and stops where the devices started. Velocity is continuous,
    # so the limits are checked on accelerations the stage really sees.
    dt = 1.0 / rate
    ends = path_positions(trajectory, np.array([0.0, dt, trajectory.duration_s - dt,
                                                trajectory.duration_s]))
    v_start = (ends[:, 1] - ends[:, 0]) / dt
    v_end = (ends[:, 3] - ends[:, 2]) / dt
    captured = dict(captured_um or {})
    approach_s = approach / rate
    positions = {}
    for row, device in enumerate(trajectory.devices):
        rest = float(captured.get(device, samples[row, 0]))
        if approach == 0 and rest != samples[row, 0]:
            raise ScheduleRefused([
                f'The path starts {device} at {samples[row, 0]:g} µm, but it is at {rest:g} '
                'µm: give the trajectory an approach time.'])
        way_in = _hermite(np.arange(approach) / max(approach, 1), rest, 0.0,
                          samples[row, 0], v_start[row], approach_s)
        way_back = _hermite(np.arange(approach + 1) / max(approach, 1), ends[row, 3],
                            v_end[row], rest, 0.0, approach_s)
        positions[device] = np.concatenate([way_in, samples[row], way_back])
    total = approach + path_ticks + approach + 1
    digital = {}
    for event, start, end in edges:
        line = digital.setdefault(event.device, np.zeros(total, dtype=bool))
        line[approach + start:approach + end] = True
    grid = Grid.covering(trajectory.devices, centres, float(trajectory.pixel_um))
    return RealizedTrajectory(
        program, rate, policy, approach, path_ticks, starts + approach, lengths, centres,
        positions, digital, grid,
        sources=[{'quantity': 'rate', 'value': rate}])


def limits_refusals(realized: RealizedTrajectory, limits: Mapping[str, Mapping]) -> List[str]:
    """The path against each device's ``vel_max`` (µm/µs) and ``acc_max``
    (µm/µs²) from its setup entry. A limit a device does not declare is
    unknown, and refused: a path is not run on limits nobody gave."""
    out = []
    rate = realized.rate_hz
    for device, positions in realized.positions_um.items():
        props = limits.get(device) or {}
        speed = (np.max(np.abs(np.diff(positions))) * rate / US_PER_S
                 if len(positions) > 1 else 0.0)
        accel = (np.max(np.abs(np.diff(positions, 2))) * (rate / US_PER_S) ** 2
                 if len(positions) > 2 else 0.0)
        for name, value, unit in (('vel_max', speed, 'µm/µs'), ('acc_max', accel, 'µm/µs²')):
            limit = props.get(name)
            if limit is None:
                out.append(f'{device} declares no {name}: its limit for a swept path is '
                           'unknown.')
            elif value > float(limit):
                out.append(f'{device} would reach {value:.4g} {unit}, above its {name} of '
                           f'{float(limit):g} {unit}. Slow the path (a longer duration) or '
                           'shrink it.')
    return out


__all__ = ['ASSUMPTIONS', 'COUNTS', 'Grid', 'GridAccumulator', 'MEAN', 'MEASUREMENTS',
           'PATH_ANCHOR', 'PATTERNS', 'Pattern', 'RealizedTrajectory', 'limits_refusals',
           'path_positions', 'realize_trajectory']


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

"""The polarization rig's old Microscope_GUI modes as scan programs, and the
old waveform generator's own output to check them against.

The rig ran on Microscope_GUI (github.com/Spexophis/Microscope_GUI, branch
``polarization_scan``, commit 66505f0): a Nucleo board played buffers of
three TTL lines (405, 488, camera) and two galvo DACs at 250 kHz, computed
by ``miao/processes/process_trigger.py``. On ImSwitch the Nucleo is a step
board (``IMSWITCH_STEP/1``) and each mode is a scan program the Expert panel
runs:

- ``wide_field``: the live Wide Field cycle, ``frames`` times;
- ``wide_field_2d``: 3 dark frames, then pairs of frames with the 405 on the
  second of each pair (the old GUI switched the 405 by serial; here its TTL
  window is left out, which the rig agreed to);
- ``galvo_scan``: galvo Y up and back once per frame, 405 on the way up,
  488 and camera on the way back;
- ``line_scan_2d``: the Wide Field cycle at every dot of a grid, passes in
  pairs with the 405 on the second.

``legacy_wide_field`` and ``legacy_galvo_scan`` transcribe, line by line,
what the old generator computed for those modes (arrays of samples, as it
uploaded them); ``legacy_line_scan_positions`` the grid its Line Scan
stepped through. The tests run each program on a simulated Nucleo and
compare its log with these, sample by sample.

What the programs do differently, on purpose:

- volts are converted with the DAC's 4095 codes over 3.3 V, where the old
  code used 4096 (and truncated), so a position may differ by one code;
- the Galvo Scan is a staircase of ``step_samples`` samples per step; the old
  DAC played one code every second sample at the default 0.4 V range, which
  ``step_samples=2`` matches;
- a Line Scan program has no dark frames: a program has one loop structure,
  and the dark frames sit before the grid at the galvos' origin. A script
  runs ``dark_frames`` first when they are wanted.

Qt-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Tuple

from .program import (
    DetectorBinding, DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait, validate,
)

STRICT = 'strict-v1'

#: The old GUI's DAC scale: counts = volts × 4096 / 3.3 (one past 12 bits at 3.3 V).
LEGACY_COUNTS_PER_VOLT = 4096 / 3.3
#: The DAC's own: 4095 codes over 3.3 V.
DAC_VOLTS_PER_CODE = 3.3 / 4095


@dataclass(frozen=True)
class LegacyParams:
    """The old panel's values (``widget_con.py``, ``process_trigger.py``).

    Defaults are the panel's spin-box defaults, except the galvo origins
    (the panel defaults to 0 V; these are ``TriggerParameters``' own) and the
    Line Scan's dot range, which the panel defaults to 0.2 V like the others.
    The rig's saved values replace them once they are known.
    """
    sample_rate: int = 250_000
    #: TTL windows in s: the 488 off-switching window, 405, 488, camera.
    off_window: Tuple[float, float] = (0.000, 0.010)
    window_405: Tuple[float, float] = (0.008, 0.032)
    window_488: Tuple[float, float] = (0.008, 0.032)
    camera_window: Tuple[float, float] = (0.008, 0.032)
    initial_time: float = 0.001
    standby_time: float = 0.002
    frame_rate: float = 30.0
    #: Galvo X, Y (V).
    origins: Tuple[float, float] = (1.0, 1.465)
    galvo_ranges: Tuple[float, float] = (0.4, 0.4)
    dot_ranges: Tuple[float, float] = (0.2, 0.2)
    dot_steps: Tuple[float, float] = (0.0172, 0.0172)

    @property
    def period(self) -> Decimal:
        return Decimal(1) / Decimal(self.sample_rate)

    def samples(self, seconds) -> int:
        """Seconds as the old code turned them into samples: truncated."""
        return int(seconds * self.sample_rate)


@dataclass(frozen=True)
class Wiring:
    """Which setup devices play the old channels, and the galvos' µm per volt
    (their ``conversionFactor``)."""
    laser_405: str = '405'
    laser_488: str = '488'
    camera: str = 'Hamamatsu'
    galvo_x: str = 'GalvoX'
    galvo_y: str = 'GalvoY'
    um_per_volt_x: float = 1.0
    um_per_volt_y: float = 1.0

    @classmethod
    def from_setup(cls, setup, **names) -> 'Wiring':
        """The wiring with each galvo's ``conversionFactor`` read from the setup."""
        wiring = cls(**names)
        factors = {}
        for axis, name in (('x', wiring.galvo_x), ('y', wiring.galvo_y)):
            info = setup.positioners[name]
            factors[f'um_per_volt_{axis}'] = float(info.managerProperties['conversionFactor'])
        return cls(**{**names, **factors})


# --- the old generator, transcribed -------------------------------------------------

@dataclass
class LegacyBuffers:
    """One upload: TTL rows (0/1) for 405, 488 and camera, and the galvo
    counts X and Y, one entry per sample."""
    ttl_405: List[int]
    ttl_488: List[int]
    camera: List[int]
    galvo_x: List[int]
    galvo_y: List[int]

    def __len__(self):
        return len(self.ttl_405)

    @staticmethod
    def volts(counts):
        """What the old firmware put out for ``counts``: 12 bits of it."""
        return [(int(c) & 0xFFF) * DAC_VOLTS_PER_CODE for c in counts]


def _digital_samples(params: LegacyParams):
    """``update_digital_parameters``: the windows' starts and ends in samples,
    in the order off, 405, 488, camera."""
    windows = (params.off_window, params.window_405, params.window_488, params.camera_window)
    return [params.samples(w[0]) for w in windows], [params.samples(w[1]) for w in windows]


def legacy_wide_field(params: LegacyParams = LegacyParams()) -> LegacyBuffers:
    """``generate_digital_triggers(lasers=[0, 1], camera=0)``, line by line."""
    starts, ends = _digital_samples(params)
    cam_ind = 2
    channels = [0, 1, cam_ind]
    initial = math.ceil(params.initial_time * params.sample_rate)
    if initial > starts[cam_ind]:
        offset = initial - starts[cam_ind]
        starts = [s + offset for s in starts]
        ends = [e + offset for e in ends]
    standby = math.ceil(params.standby_time * params.sample_rate)
    cycle = max(ends[-1] + standby + 2, max(ends[i] for i in channels))
    cycle = max(math.ceil(params.sample_rate / params.frame_rate), cycle)
    rows = [[0] * cycle for _ in channels]
    for ln, ch in enumerate(channels):
        for k in range(starts[ch + 1], ends[ch + 1]):
            rows[ln][k] = 1
    for k in range(starts[0], ends[0]):       # the 488 off-switching window
        rows[1][k] = 1
    for row in rows:
        row[-1] = 0
    x = int(params.origins[0] * LEGACY_COUNTS_PER_VOLT)
    y = int(params.origins[1] * LEGACY_COUNTS_PER_VOLT)
    return LegacyBuffers(rows[0], rows[1], rows[2], [x] * cycle, [y] * cycle)


def _galvo_y_limits(params: LegacyParams):
    """``update_galvo_scan_parameters``: galvo Y's start and stop (V)."""
    origin, rng = params.origins[1], params.galvo_ranges[1]
    return origin - rng / 2, origin + rng / 2


def _linspace(a, b, n):
    """numpy.linspace(a, b, n) for n >= 2: the same floats."""
    step = (b - a) / (n - 1)
    values = [a + i * step for i in range(n)]
    values[-1] = b
    return values


def _galvo_scan_layout(params: LegacyParams):
    """The Galvo Scan's sample counts, as ``generate_galvo_scanning_triggers``
    computes them."""
    dt = 1 / params.sample_rate
    band_width = 125
    exposure = int(2e-3 / dt)
    period = int((1 / band_width) / dt)
    delay = 200
    half = int(period / 2)
    final = int((1 / params.frame_rate) / dt)
    start_405 = 250
    end_405 = start_405 + exposure
    start_488 = half + delay + 125 + (half - end_405)
    return dict(exposure=exposure, delay=delay, half=half, final=final,
                window_405=(start_405, end_405), window_488=(start_488, start_488 + exposure))


def legacy_galvo_scan(params: LegacyParams = LegacyParams()) -> LegacyBuffers:
    """``generate_galvo_scanning_triggers(lasers=[0, 1], camera=0)``, line by line."""
    lay = _galvo_scan_layout(params)
    start, stop = _galvo_y_limits(params)
    half, final = lay['half'], lay['final']
    y = [stop] * (half + int(lay['delay'] / 2))
    y[:half] = _linspace(start, stop, half)
    yn = y + y[::-1]
    yf = yn + [start] * (final - len(yn))
    x = [params.origins[0]] * final
    rows = [[0] * final for _ in range(3)]
    for k in range(*lay['window_405']):
        rows[0][k] = 1
    for k in range(*lay['window_488']):
        rows[1][k] = 1
        rows[2][k] = 1
    return LegacyBuffers(rows[0], rows[1], rows[2],
                         [int(v * LEGACY_COUNTS_PER_VOLT) for v in x],
                         [int(v * LEGACY_COUNTS_PER_VOLT) for v in yf])


def _arange(start, stop, step):
    """numpy.arange for floats: as many values as ceil((stop - start) / step)."""
    n = max(0, math.ceil((stop - start) / step))
    return [start + i * step for i in range(n)]


def legacy_line_scan_positions(params: LegacyParams = LegacyParams()):
    """``prepare_line_scan``: the (x, y) volts it stepped through, Y fastest."""
    dot_starts = [o - r / 2 for o, r in zip(params.origins, params.dot_ranges)]
    dot_stops = [o + r / 2 for o, r in zip(params.origins, params.dot_ranges)]
    xs = _arange(dot_starts[0], dot_stops[0] + 0.0001, params.dot_steps[0])
    ys = _arange(dot_starts[1], dot_stops[1] + 0.0001, params.dot_steps[1])
    return [(x, y) for x in xs for y in ys], len(xs), len(ys)


# --- the programs --------------------------------------------------------------------

def _s(params: LegacyParams, samples) -> float:
    """``samples`` of the old clock in s, exact on the decimal grid."""
    return float(Decimal(samples) * params.period)


def _held(device, volts, um_per_volt) -> DeviceDrive:
    return DeviceDrive(device, offset_um=volts * um_per_volt, motion=Motion(
        transition='jump', origin='absolute', restore='hold'))


def _cycle_events(params: LegacyParams, wiring: Wiring, *, phases_405=None, phases_488=None):
    """The Wide Field cycle's windows, at the samples the old generator used."""
    buffers = legacy_wide_field(params)
    events = []
    for device, row, phases, tag in ((wiring.laser_405, buffers.ttl_405, phases_405, '405'),
                                     (wiring.laser_488, buffers.ttl_488, phases_488, '488')):
        for index, (a, b) in enumerate(_runs(row)):
            events.append(TimelineEvent(device, 'ttl', _s(params, a), _s(params, b),
                                        phases=phases, during='any', id=f'{tag}-{index}'))
    (a, b), = _runs(buffers.camera)
    events.append(TimelineEvent(wiring.camera, 'detector', _s(params, a), _s(params, b),
                                during='any', id='exposure'))
    return events, len(buffers)


def _runs(row) -> List[Tuple[int, int]]:
    """The [start, end) sample ranges where ``row`` is 1."""
    runs, start = [], None
    for k, v in enumerate(list(row) + [0]):
        if v and start is None:
            start = k
        elif not v and start is not None:
            runs.append((start, k))
            start = None
    return runs


def wide_field(params: LegacyParams = LegacyParams(), wiring: Wiring = Wiring(), *,
               frames: int = 1) -> ScanProgram:
    """The live Wide Field cycle, ``frames`` times; the panel's Repeat makes it live."""
    events, cycle = _cycle_events(params, wiring)
    return validate(ScanProgram(
        loops=(Loop('frame', 'time', int(frames)),),
        drives=(_held(wiring.galvo_x, params.origins[0], wiring.um_per_volt_x),
                _held(wiring.galvo_y, params.origins[1], wiring.um_per_volt_y)),
        step_s=_s(params, cycle), timeline=tuple(events),
        detectors=(DetectorBinding(wiring.camera, events='exposure'),),
        timing_policy=STRICT, approach=False))


def wide_field_2d(params: LegacyParams = LegacyParams(), wiring: Wiring = Wiring(), *,
                  pairs: int, dark_frames: int = 3) -> ScanProgram:
    """``dark_frames`` frames with no laser, then ``pairs`` pairs of frames:
    the 488 on in every one, the 405 on in the second of each pair."""
    pairs, dark = int(pairs), int(dark_frames)
    lit = frozenset(range(dark, dark + 2 * pairs))
    with_405 = frozenset(range(dark + 1, dark + 2 * pairs, 2))
    events, cycle = _cycle_events(params, wiring, phases_405=with_405, phases_488=lit)
    return validate(ScanProgram(
        loops=(Loop('frame', 'condition', dark + 2 * pairs),),
        drives=(_held(wiring.galvo_x, params.origins[0], wiring.um_per_volt_x),
                _held(wiring.galvo_y, params.origins[1], wiring.um_per_volt_y)),
        step_s=_s(params, cycle), timeline=tuple(events),
        detectors=(DetectorBinding(wiring.camera, events='exposure'),),
        timing_policy=STRICT, approach=False))


def dark_frames(params: LegacyParams = LegacyParams(), wiring: Wiring = Wiring(), *,
                frames: int = 3) -> ScanProgram:
    """The camera's frames with every laser off, the galvos at their origin:
    what the old Line Scan played before its grid."""
    _, cycle = _cycle_events(params, wiring)
    buffers = legacy_wide_field(params)
    (a, b), = _runs(buffers.camera)
    return validate(ScanProgram(
        loops=(Loop('frame', 'time', int(frames)),),
        drives=(_held(wiring.galvo_x, params.origins[0], wiring.um_per_volt_x),
                _held(wiring.galvo_y, params.origins[1], wiring.um_per_volt_y)),
        step_s=_s(params, cycle),
        timeline=(TimelineEvent(wiring.camera, 'detector', _s(params, a), _s(params, b),
                                during='any', id='exposure'),),
        detectors=(DetectorBinding(wiring.camera, events='exposure'),),
        timing_policy=STRICT, approach=False))


def _window_events(device, kind, window, step, tag):
    """A window of old samples [a, b) as step events: the steps it covers
    whole (their phases), and the parts of the steps it starts or ends in."""
    a, b = window
    events = []
    first_whole, last_whole = -(-a // step), b // step       # steps [first, last) are whole
    if first_whole > last_whole:                              # inside one step
        return [(device, kind, a % step, b - (a // step) * step, {a // step}, tag)]
    if a % step:
        events.append((device, kind, a % step, step, {a // step}, f'{tag}-head'))
    if last_whole > first_whole:
        events.append((device, kind, 0, step, set(range(first_whole, last_whole)), tag))
    if b % step:
        events.append((device, kind, 0, b % step, {last_whole}, f'{tag}-tail'))
    return events


def galvo_scan(params: LegacyParams = LegacyParams(), wiring: Wiring = Wiring(), *,
               frames: int = 1, step_samples: int = 2) -> ScanProgram:
    """Galvo Y up, held, and back once per frame, the 405 window on the way
    up, the 488 and camera windows on the way back, a time wait for the rest
    of the frame.

    The waveform is the old one -- the very DAC codes the old firmware put
    out -- sampled every ``step_samples`` samples into a table: one step per
    entry. That loop is the phase loop, so a window is the steps it covers --
    an event anchored to a pass would have to fit in one step
    (``schedule.py``: "leaves its step")."""
    lay = _galvo_scan_layout(params)
    step = int(step_samples)
    legacy = legacy_galvo_scan(params)
    half = lay['half']
    hold = int(lay['delay'] / 2) * 2
    moving = 2 * half + hold                       # up, hold, down: the rest stands at start
    if moving % step:
        raise ValueError(f'The sweep of {moving} samples does not divide into steps of {step}.')
    count = moving // step
    volts = LegacyBuffers.volts(legacy.galvo_y)
    um_y = wiring.um_per_volt_y
    table = tuple(volts[i * step] * um_y for i in range(count))
    events = []
    for device, kind, window, tag in (
            (wiring.laser_405, 'ttl', lay['window_405'], '405'),
            (wiring.laser_488, 'ttl', lay['window_488'], '488'),
            (wiring.camera, 'detector', lay['window_488'], 'exposure')):
        for dev, k, a, b, phases, ident in _window_events(device, kind, window, step, tag):
            events.append(TimelineEvent(dev, k, _s(params, a), _s(params, b),
                                        phases=frozenset(phases), during='any', id=ident))
    rest = lay['final'] - moving
    assert len(legacy) == lay['final']
    return validate(ScanProgram(
        loops=(Loop('frame', 'time', int(frames)), Loop('t', 'condition', count)),
        drives=(_held(wiring.galvo_x, params.origins[0], wiring.um_per_volt_x),
                DeviceDrive(wiring.galvo_y, table_loop='t', table_um=table,
                            motion=Motion(transition='jump', origin='absolute',
                                          restore='first'))),
        step_s=_s(params, step), timeline=tuple(events),
        waits=(Wait(at='frame', trigger='time', interval_s=_s(params, rest)),) if rest > 0 else (),
        detectors=(DetectorBinding(wiring.camera, events=wiring.camera),),
        timing_policy=STRICT, approach=False))


def line_scan_2d(params: LegacyParams = LegacyParams(), wiring: Wiring = Wiring(), *,
                 pairs: int) -> ScanProgram:
    """The Wide Field cycle at every dot of the grid, Y fastest, in ``pairs``
    pairs of passes: the 488 in every pass, the 405 in the second of each."""
    pairs = int(pairs)
    positions, nx, ny = legacy_line_scan_positions(params)
    (x0, y0) = positions[0]
    with_405 = frozenset(range(1, 2 * pairs, 2))
    events, cycle = _cycle_events(params, wiring, phases_405=with_405)
    motion = Motion(transition='jump', origin='absolute', restore='first')
    return validate(ScanProgram(
        loops=(Loop('pass', 'condition', 2 * pairs), Loop('x', 'scan_x', nx),
               Loop('y', 'scan_y', ny)),
        drives=(DeviceDrive(wiring.galvo_x, offset_um=x0 * wiring.um_per_volt_x,
                            weights_um={'x': params.dot_steps[0] * wiring.um_per_volt_x},
                            motion=motion),
                DeviceDrive(wiring.galvo_y, offset_um=y0 * wiring.um_per_volt_y,
                            weights_um={'y': params.dot_steps[1] * wiring.um_per_volt_y},
                            motion=motion)),
        step_s=_s(params, cycle), timeline=tuple(events),
        detectors=(DetectorBinding(wiring.camera, events='exposure'),),
        timing_policy=STRICT, approach=False))


# --- comparing a run with the old buffers --------------------------------------------

def sampled(board_log_entries, *, channels: Mapping[str, str], grid_ns: int,
            period_s: float, samples: int, start_tick: int = 0) -> Dict[str, List[float]]:
    """A board's log as the old sample arrays: each channel's value at the
    start of every ``period_s`` sample from ``start_tick``. ``channels`` maps
    the board's channel names (``dac0``, ``ttl1``) to array names."""
    per_sample = round(period_s * 1e9 / grid_ns)
    changes = {name: [] for name in channels}
    for kind, *fields in board_log_entries:
        if kind == 'dac':
            _segment, tick, channel, value = fields
        elif kind in ('on', 'off'):
            _segment, tick, channel, value = fields
        else:
            continue
        if channel in changes:
            changes[channel].append((tick, float(value)))
    out = {}
    for channel, name in channels.items():
        edges = sorted(changes[channel], key=lambda e: e[0])
        value, index, values = 0.0, 0, []
        for k in range(samples):
            tick = start_tick + k * per_sample
            while index < len(edges) and edges[index][0] <= tick:
                value = edges[index][1]
                index += 1
            values.append(value)
        out[name] = values
    return out


__all__ = [
    'DAC_VOLTS_PER_CODE', 'LEGACY_COUNTS_PER_VOLT', 'LegacyBuffers', 'LegacyParams', 'Wiring',
    'dark_frames', 'galvo_scan', 'legacy_galvo_scan', 'legacy_line_scan_positions',
    'legacy_wide_field', 'line_scan_2d', 'sampled', 'wide_field', 'wide_field_2d',
]


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

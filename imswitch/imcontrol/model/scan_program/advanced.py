"""Advanced scans as scan programs (docs/design/plans/
scan-engines-and-expert-scan.md §2.3, §2.6, M0).

``program_from_advanced(stage, digital, setup)`` turns what the Advanced
controller hands its designers -- the stage parameters
``BetaScanDesigner`` receives, and the digital parameters
``AdvancedScanTTLCycleDesigner`` receives -- into a program with the same
meaning:

- loops: slow (when emitted), middle, the line-step phase loop, fast;
- drives with Beta's motion: a smooth ramp and a settle at the end of every
  dwell, a line return, positions relative to the position captured before
  the scan, restored to it at the end (to the first positions without one);
- every TTL window as an event with ``during: 'any'``: today's scans expose
  through the move (§2.2), and stay so;
- the TTL designer's line and frame clocks as marker events;
- ``timing_policy: 'advanced-compatible-v1'``, and the request itself in
  ``requested_as``, so saving never rewrites a requested value.

Swept (Galvo) scans are not converted here: their fast axis compiles
through ``GalvoScanDesigner`` (plan §2.7).

Qt-free.
"""

from __future__ import annotations

import copy
from typing import Mapping, Optional, Tuple

from ..scan_parameters import pixels_for_length_step
from ..signaldesigners.BetaScanDesigner import DEFAULT_MOVE_TIME_S, DEFAULT_SETTLE_TIME_S
from .interpreter import RunContext
from .policy import PolicyError, resolve_policy
from .program import DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, validate

FORMAT = 'advanced-scan/1'
COMPATIBLE_POLICY = 'advanced-compatible-v1'

#: AdvancedScanTTLCycleDesigner's clock pulse length, in samples
#: (``clock_len=10`` in its ``__generate_all_clocks``): the line, frame-start
#: and frame-end clocks it always emits. A fact about that designer, kept so
#: the program describes what it plays.
TTL_DESIGNER_CLOCK_SAMPLES = 10

#: AdvancedScanTTLCycleDesigner accepts a window ending up to this far past
#: the dwell (``b > dwell_s + 1e-12`` raises) and clips it to the pixel. The
#: adapter clips the same windows, so the program carries what it plays.
TTL_DESIGNER_END_SLACK_S = 1e-12

CLOCKS = ('line_clock', 'frame_start_clock', 'frame_end_clock')


class AdvancedConversionError(ValueError):
    """An Advanced scan the program cannot (yet) express."""


def _scanning_props(setup):
    return {name: info.managerProperties for name, info in setup.positioners.items()
            if getattr(info, 'forScanning', False)}


def program_from_advanced(stage: Mapping, digital: Optional[Mapping], setup
                          ) -> Tuple[ScanProgram, RunContext]:
    """The program and run context for a Beta-designed Advanced scan."""
    stage = dict(stage)
    targets = list(stage['target_device'])
    if len(targets) != 3:
        raise AdvancedConversionError('The Beta designer scans exactly three target devices.')
    props = _scanning_props(setup)
    conv = [float(props[name].get('conversionFactor', 1)) for name in targets]
    lengths = [float(v) for v in stage['axis_length']]
    steps = [float(v) for v in stage['axis_step_size']]
    centres = [float(v) for v in stage['axis_centerpos']]
    n_linesteps = max(1, int(stage.get('n_linesteps', 1)))
    snapshot = stage.get('axis_position_before_scan')
    anchor = stage.get('position_anchor', 'center')

    # Pixel counts exactly as the Beta designer computes them (in its units).
    counts = [1 if lengths[i] / conv[i] == 0 else
              pixels_for_length_step(lengths[i] / conv[i], steps[i] / conv[i]) for i in range(3)]
    slow_emitted = lengths[2] / conv[2] > 0

    motion = Motion(
        transition='smooth',
        move_s=float(stage.get('move_time', DEFAULT_MOVE_TIME_S)),
        settle_s=float(stage.get('settle_time', DEFAULT_SETTLE_TIME_S)),
        placement='in-step',
        return_s=float(stage['return_time']),
        origin='captured' if snapshot is not None else 'absolute',
        restore='captured' if snapshot is not None else 'first',
    )
    loop_ids = ['x', 'y', 'z']
    kinds = ['scan_x', 'scan_y', 'scan_z']
    loops = []
    if slow_emitted:
        loops.append(Loop('z', kinds[2], counts[2]))
    loops.append(Loop('y', kinds[1], counts[1]))
    if n_linesteps > 1:
        loops.append(Loop('phase', 'condition', n_linesteps))
    loops.append(Loop('x', kinds[0], counts[0]))

    drives = []
    for i in range(3 if slow_emitted else 2):
        if snapshot is not None and anchor == 'start':
            offset = centres[i]
        else:
            offset = centres[i] - (counts[i] - 1) * steps[i] / 2.0
        drives.append(DeviceDrive(targets[i], offset_um=offset,
                                  weights_um={loop_ids[i]: steps[i]}, motion=motion))

    captured = {}
    if snapshot is not None:
        captured = {targets[i]: float(snapshot[i][0]) for i in range(3)}

    timeline = _timeline(digital, setup, float(stage['sequence_time']), n_linesteps)
    program = ScanProgram(
        loops=tuple(loops), drives=tuple(drives), step_s=float(stage['sequence_time']),
        timeline=tuple(timeline), timing_policy=COMPATIBLE_POLICY,
        requested_as={'format': FORMAT, 'stage': copy.deepcopy(dict(stage)),
                      'digital': copy.deepcopy(dict(digital)) if digital else None},
    )
    return validate(program), RunContext(captured_um=captured)


def _timeline(digital, setup, dwell, n_linesteps):
    if not digital:
        return []
    rate = float(setup.scan.sampleRate)
    scanning = set(_scanning_props(setup))
    advanced = bool(digital.get('advanced_mode', False))
    phases = int(digital.get('n_linesteps', n_linesteps))
    if phases != n_linesteps:
        raise AdvancedConversionError(
            f'The TTL program has {phases} line steps, the scan {n_linesteps}.')
    events = []
    for device in digital['target_device']:
        if device in scanning:
            continue
        enables = list(map(bool, digital['linestep_enable'].get(device, [False] * phases)))
        if len(enables) != phases:
            raise AdvancedConversionError(
                f'{device}: per-line enables ({len(enables)} for {phases} line steps) '
                'are not expressible yet.')
        starts = digital['pulse_starts_s'].get(device, [[] for _ in range(phases)])
        ends = digital['pulse_ends_s'].get(device, [[] for _ in range(phases)])
        for phase in range(phases):
            if not enables[phase]:
                continue
            which = frozenset({phase}) if phases > 1 else None
            windows = list(zip(starts[phase] if phase < len(starts) else [],
                               ends[phase] if phase < len(ends) else []))
            if not advanced or not windows:
                events.append(TimelineEvent(device, 'ttl', 0.0, dwell, phases=which,
                                            during='any'))
                continue
            for a, b in windows:
                if a is None or b is None:
                    continue
                if a < 0 or b < 0 or a >= b or b > dwell + TTL_DESIGNER_END_SLACK_S:
                    raise AdvancedConversionError(
                        f'{device}: the window {a}-{b} s does not fit the {dwell} s dwell.')
                events.append(TimelineEvent(device, 'ttl', float(a), min(float(b), dwell),
                                            phases=which, during='any'))
    clock = TTL_DESIGNER_CLOCK_SAMPLES / rate
    events.append(TimelineEvent('line_clock', 'marker', 0.0, clock, during='any',
                                anchor='pass-start:x'))
    events.append(TimelineEvent('frame_start_clock', 'marker', 0.0, clock, during='any',
                                anchor='pass-start:y'))
    events.append(TimelineEvent('frame_end_clock', 'marker', -clock, 0.0, during='any',
                                anchor='pass-end:y'))
    return events


def _rising_edges(intervals):
    """Rising edges of a line whose on-intervals are given: touching
    intervals are one pulse, as on the wire."""
    edges, end = 0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            edges += 1
        end = stop if end is None else max(end, stop)
    return edges


def layout_inputs(schedule, detector_names):
    """What ``build_advanced_scan_layouts`` reads, derived from the program
    and its realized schedule instead of from the designers' scanInfo: the
    physical axes, the phase masks and the pulses per step and phase. The
    controller hands them to ``build_advanced_scan_layouts``, which lives with
    the controllers; the model does not import it."""
    program = schedule.expansion.program
    scan_loops = [loop for loop in program.loops if loop.kind.startswith('scan_')]
    fast_to_slow = list(reversed(scan_loops))
    drives = {}
    for drive in program.drives:
        for loop_id in drive.weights_um:
            drives[loop_id] = drive
    labels = ['x', 'y', 'z'][:len(fast_to_slow)]
    scan_info = {
        'img_dims': [loop.count for loop in fast_to_slow],
        'img_axes_phys': labels,
        'pixel_sizes': [float(drives[loop.id].weights_um[loop.id]) if loop.id in drives else None
                        for loop in fast_to_slow],
        'n_linesteps': program.phase_count,
    }
    devices = {label: drives[loop.id].device for label, loop in zip(labels, fast_to_slow)
               if loop.id in drives}
    phases = program.phase_count
    first_step = {}
    for realized in schedule.step_spans():
        first_step.setdefault(realized.span.phase, realized)
    masks, counts = {}, {}
    for detector in detector_names:
        per_phase = []
        for phase in range(phases):
            step = first_step.get(phase)
            intervals = [(e.start_tick, e.end_tick) for e in schedule.events
                         if step is not None and e.device == detector
                         and e.step_index == step.span.step_index]
            per_phase.append(_rising_edges(intervals))
        if any(per_phase):
            masks[detector] = [bool(count) for count in per_phase]
            counts[detector] = tuple(per_phase)
    return scan_info, devices, masks, counts


def advanced_from_program(program: ScanProgram):
    """The Advanced request a program was made from, unchanged."""
    request = program.requested_as or {}
    if request.get('format') != FORMAT:
        raise AdvancedConversionError('This program was not made from an Advanced scan.')
    return copy.deepcopy(request['stage']), copy.deepcopy(request['digital'])


# ---------------------------------------------------------------------------
# Saved scans: which timing policy applies (§2.6)
# ---------------------------------------------------------------------------

def is_legacy_advanced_state(state) -> bool:
    """Whether ``state`` parses as today's Advanced scan-state format: its
    analog and digital dicts with the keys the Advanced designers read."""
    if not isinstance(state, Mapping):
        return False
    analog = state.get('analog') if 'analog' in state else state.get('analogParameterDict')
    digital = state.get('digital') if 'digital' in state else state.get('digitalParameterDict')
    if not (isinstance(analog, Mapping) and isinstance(digital, Mapping)):
        return False
    return ({'target_device', 'axis_length', 'axis_step_size', 'axis_centerpos',
             'sequence_time'} <= set(analog)
            and {'target_device', 'n_linesteps', 'linestep_enable'} <= set(digital))


def timing_policy_for_state(state):
    """The timing policy a saved scan runs under.

    A saved ``timingPolicy`` is used (an unknown one is refused). A
    recognized legacy Advanced file without one runs under
    ``advanced-compatible-v1``. Anything else without a valid policy is
    refused: a malformed new-format program gains no compatibility exemption.
    """
    if isinstance(state, Mapping) and 'timingPolicy' in state:
        return resolve_policy(state['timingPolicy'])
    if is_legacy_advanced_state(state):
        return resolve_policy(COMPATIBLE_POLICY)
    raise PolicyError('This scan names no timing policy and is not a recognized legacy '
                      'Advanced scan; it is refused rather than run under an assumed policy.')


__all__ = [
    'AdvancedConversionError', 'COMPATIBLE_POLICY', 'FORMAT', 'advanced_from_program',
    'is_legacy_advanced_state', 'layout_inputs', 'program_from_advanced',
    'timing_policy_for_state',
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

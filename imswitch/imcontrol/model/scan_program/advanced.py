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
import dataclasses
import re
from typing import List, Mapping, Optional, Tuple

from ..errors import ScanDesignRefusedError
from ..scan_parameters import pixels_for_length_step
from ..signaldesigners.BetaScanDesigner import DEFAULT_MOVE_TIME_S, DEFAULT_SETTLE_TIME_S
from .densify import densify
from .interpreter import InterpretError, RunContext, expand
from .policy import PolicyError, resolve_policy
from .program import (
    DeviceDrive, Loop, Motion, ProgramError, ScanProgram, TimelineEvent, validate,
)
from .schedule import ScheduleRefused, format_seconds, realize, realized_duration

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

#: The scanInfoDict key that tells point detectors where each visited pixel,
#: line and slice belongs (plan M2): one entry per image axis (x, y, z),
#: ``None`` for forward, ``{'interleave': [image index per visit]}`` or
#: ``{'serpentine': 'line'}`` (x: every other line played in a slice, line
#: steps included, runs back) / ``{'serpentine': 'slice'}`` (y: every other
#: slice).
AXIS_ORDER_KEY = 'axis_visit_order'

STRICT_POLICY = 'strict-v1'

#: The designer the program compiler builds Advanced scans on (plan M2):
#: stepped, every pixel a dwell. Swept designers compile their fast axis
#: themselves (plan §2.7) and take no visit orders yet.
PROGRAM_DESIGNER = 'BetaScanDesigner'


def parse_axis_order(value):
    """``('forward', None)``, ``('serpentine', None)`` or ``('interleave', k)``
    from an ``axis_order`` entry (``'interleave:3'``)."""
    text = str(value or 'forward').strip().lower()
    if text in ('forward', 'serpentine'):
        return text, None
    if text.startswith('interleave:'):
        try:
            return 'interleave', int(text.split(':', 1)[1])
        except ValueError:
            pass
    raise AdvancedConversionError(f'Unknown axis order {value!r}: forward, serpentine or '
                                  'interleave:<k>.')


def interleave_permutation(count, k):
    """The image index of each visit when ``count`` positions are visited
    ``k`` at a time apart: 0, k, 2k, ... then 1, k+1, ... -- position =
    cycle + k * plane, the cycles of MS-RESOLFT."""
    if k < 2 or k >= count or count % k:
        raise AdvancedConversionError(
            f'An interleave of {k} needs a number of positions that it divides, above {k}; '
            f'this axis has {count}.')
    return [cycle + k * plane for cycle in range(k) for plane in range(count // k)]


def uses_program_features(stage: Mapping, digital: Optional[Mapping]) -> bool:
    """Whether an Advanced scan uses an option only the program compiler
    builds (plan M2): an axis order other than forward, or intra-pixel moves."""
    if any(parse_axis_order(v)[0] != 'forward' for v in stage.get('axis_order') or ()):
        return True
    return bool(_offset_commands(digital, max(1, int(stage.get('n_linesteps', 1)))))


def _offset_commands(digital, n_linesteps):
    """Guillaume's intra-pixel line program, as ``[(device, step, start, end,
    µm)]``: every line step it is enabled in, every window."""
    digital = digital or {}
    if not digital.get('intra_pixel_positioner_movement'):
        return []
    commands = []
    enable = digital.get('positioner_linestep_enable') or {}
    starts = digital.get('positioner_movement_starts_s') or {}
    ends = digital.get('positioner_movement_ends_s') or {}
    sizes = digital.get('positioner_step_size_um') or {}
    for device in digital.get('positioner_target_device') or ():
        for step in range(n_linesteps):
            flags = list(enable.get(device) or [])
            if step >= len(flags) or not flags[step]:
                continue
            step_starts = list((starts.get(device) or [[]] * n_linesteps)[step] or [])
            step_ends = list((ends.get(device) or [[]] * n_linesteps)[step] or [])
            step_sizes = list((sizes.get(device) or [[]] * n_linesteps)[step] or [])
            for index, (a, b) in enumerate(zip(step_starts, step_ends)):
                if index < len(step_sizes):
                    size = step_sizes[index]
                elif step_sizes:
                    size = step_sizes[-1]
                else:
                    raise AdvancedConversionError(
                        f'{device}: the intra-pixel move in line step {step + 1} has no step '
                        'size.')
                commands.append((device, step, float(a), float(b), float(size)))
    return commands


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
        # Without a return time the designer's waveforms end at the last
        # position (the Advanced controller parks the stage by a command
        # afterwards, outside the stream); the program says what the stream
        # does. With one, the last return takes the stage back.
        restore='hold' if float(stage['return_time']) == 0 else
        ('captured' if snapshot is not None else 'first'),
    )
    loop_ids = ['x', 'y', 'z']
    kinds = ['scan_x', 'scan_y', 'scan_z']
    orders = list(stage.get('axis_order') or ['forward'] * 3)
    orders += ['forward'] * (3 - len(orders))
    parsed = [parse_axis_order(order) for order in orders[:3]]
    # A serpentine runs back on every other pass of the loops right outside
    # it: x on every other line played in a slice (line steps included), y
    # on every other slice.
    outer = {'x': ('y', 'phase') if n_linesteps > 1 else ('y',),
             'y': ('z',) if slow_emitted else (), 'z': ()}

    def loop_for(i):
        kind, _ = parsed[i]
        if kind == 'serpentine':
            parity = outer[loop_ids[i]]
            if not parity:
                raise AdvancedConversionError(
                    f'{targets[i]}: a serpentine order alternates with the axis outside it; '
                    'the outermost axis has none.')
            return Loop(loop_ids[i], kinds[i], counts[i], traversal='serpentine',
                        parity_loops=parity)
        return Loop(loop_ids[i], kinds[i], counts[i])

    loops = []
    if slow_emitted:
        loops.append(loop_for(2))
    loops.append(loop_for(1))
    if n_linesteps > 1:
        loops.append(Loop('phase', 'condition', n_linesteps))
    loops.append(loop_for(0))

    drives = []
    for i in range(3 if slow_emitted else 2):
        if snapshot is not None and anchor == 'start':
            offset = centres[i]
        else:
            offset = centres[i] - (counts[i] - 1) * steps[i] / 2.0
        kind, k = parsed[i]
        if kind == 'interleave':
            order = interleave_permutation(counts[i], k)
            drives.append(DeviceDrive(targets[i], offset_um=offset, table_loop=loop_ids[i],
                                      table_um=tuple(index * steps[i] for index in order),
                                      motion=motion))
        else:
            drives.append(DeviceDrive(targets[i], offset_um=offset,
                                      weights_um={loop_ids[i]: steps[i]}, motion=motion))

    captured = {}
    if snapshot is not None:
        captured = {targets[i]: float(snapshot[i][0]) for i in range(3)}

    timeline = _timeline(digital, setup, float(stage['sequence_time']), n_linesteps)
    driven = {d.device for d in drives}
    for device, step, a, b, size in _offset_commands(digital, n_linesteps):
        if device not in driven:
            raise AdvancedConversionError(
                f'{device} takes an intra-pixel move but is not driven by this scan.')
        timeline.append(TimelineEvent(
            device, 'offset', a, b, value=size, shape='hold',
            phases=frozenset({step}) if n_linesteps > 1 else None))
    # A scan that uses an option only the program compiler builds runs under
    # strict timing as a whole (plan §2.6): no value is rounded silently.
    policy = STRICT_POLICY if uses_program_features(stage, digital) else COMPATIBLE_POLICY
    program = ScanProgram(
        loops=tuple(loops), drives=tuple(drives), step_s=float(stage['sequence_time']),
        timeline=tuple(timeline), timing_policy=policy,
        requested_as={'format': FORMAT, 'stage': copy.deepcopy(dict(stage)),
                      'digital': copy.deepcopy(dict(digital)) if digital else None},
        # The Beta designer has no approach: its first sample is the first
        # position (§7.9).
        approach=False,
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
    for device in digital.get('target_device') or ():
        if device in scanning:
            continue
        enables = list(map(bool, (digital.get('linestep_enable') or {}).get(
            device, [False] * phases)))
        if len(enables) != phases:
            raise AdvancedConversionError(
                f'{device}: per-line enables ({len(enables)} for {phases} line steps) '
                'are not expressible yet.')
        starts = (digital.get('pulse_starts_s') or {}).get(device, [[] for _ in range(phases)])
        ends = (digital.get('pulse_ends_s') or {}).get(device, [[] for _ in range(phases)])
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


def axis_visit_orders(stage: Mapping, img_dims):
    """``scanInfoDict[AXIS_ORDER_KEY]`` for an Advanced scan: per image axis
    (x, y, z) where each visit belongs, for the point detectors. ``img_dims``
    are the scan's pixel counts (the designer's)."""
    orders = list(stage.get('axis_order') or [])
    out = []
    for i, count in enumerate(img_dims):
        kind, k = parse_axis_order(orders[i] if i < len(orders) else 'forward')
        if kind == 'forward':
            out.append(None)
        elif kind == 'serpentine':
            if i >= 2:
                raise AdvancedConversionError(
                    'A serpentine order alternates with the axis outside it; the slow axis '
                    'has none.')
            out.append({'serpentine': ('line', 'slice')[i]})
        else:
            out.append({'interleave': interleave_permutation(int(count), k)})
    return out


def axis_order_refusal(setup) -> str:
    """Why this setup cannot scan an axis out of order, or ``''``."""
    designer = getattr(setup.scan, 'scanDesigner', None)
    if designer == PROGRAM_DESIGNER:
        return ''
    return (f'Serpentine and interleaved orders and intra-pixel moves are built for stepped '
            f'scans ({PROGRAM_DESIGNER}); this setup scans with {designer}, which sweeps its '
            'fast axis.')


def program_scan_signals(stage: Mapping, setup, designed_signals: Mapping, scan_info: Mapping,
                         digital: Optional[Mapping] = None):
    """``(signals, scan_info)`` for an Advanced scan that uses an option only
    the program compiler builds (plan M2): the analog waveforms in volts from
    the interpreter, on the Beta designer's sample structure, which they
    share -- an order or an intra-pixel move changes positions, not sample
    counts. ``digital``: the line program the TTL designer plays (with the
    positioners' intra-pixel keys); its windows are checked with the rest.
    The scan runs under strict timing as a whole: a time off the sample
    clock is refused, not rounded -- the TTL windows included, which the TTL
    designer would otherwise round (§7.9). Raises
    :class:`ScanDesignRefusedError` with the reason."""
    refusal = axis_order_refusal(setup)
    if refusal:
        raise ScanDesignRefusedError(refusal + ' Scan forward, without intra-pixel moves.')
    try:
        program, context = program_from_advanced(stage, stage if digital is None else digital,
                                                 setup)
        schedule = realize(expand(program, context), setup.scan.sampleRate,
                           rate_source='scan.sampleRate')
    except (AdvancedConversionError, ProgramError, InterpretError, ScheduleRefused,
            PolicyError) as error:
        raise ScanDesignRefusedError(str(error)) from error
    analog = densify(schedule)['analog']
    if sorted(analog) != sorted(designed_signals):
        raise ScanDesignRefusedError(
            f'The program drives {", ".join(sorted(analog))}; the scan drives '
            f'{", ".join(sorted(designed_signals))}.')
    total = int(scan_info['scan_samples_total'])
    props = _scanning_props(setup)
    signals = {}
    for device in designed_signals:
        positions_um = analog[device]
        if positions_um.size != total:
            raise ScanDesignRefusedError(
                f'{device}: the program built {positions_um.size} samples, the scan has '
                f'{total}; this combination cannot be built yet.')
        # Volts as the Beta designer converts them.
        signals[device] = positions_um / props[device].get('conversionFactor', 1)
    info = dict(scan_info)
    # The designer's compliance check reads these against the scanners'
    # ranges, as for every Beta scan.
    info['minmaxes'] = [[float(v.min()), float(v.max())] for v in signals.values()]
    try:
        info[AXIS_ORDER_KEY] = axis_visit_orders(stage, info['img_dims'])
    except AdvancedConversionError as error:
        raise ScanDesignRefusedError(str(error)) from error
    info['timing_policy'] = schedule.policy.id
    return signals, info


def scan_timing_policy(stage: Mapping) -> str:
    """The timing policy an Advanced scan runs under (plan §2.6):
    ``strict-v1`` once it uses an option only the program compiler builds,
    ``advanced-compatible-v1`` -- what the designers play -- otherwise.
    ``stage`` is what the scan designer receives (with the line program)."""
    try:
        return STRICT_POLICY if uses_program_features(stage, stage) else COMPATIBLE_POLICY
    except AdvancedConversionError:
        return STRICT_POLICY   # an order is set, if not one that can be built


def timing_note(stage: Mapping, rate_hz, digital: Optional[Mapping] = None,
                setup=None) -> str:
    """What the Advanced panel says about the scan's timing, or ``''``:
    the realized dwell beside the requested one when the designers round it,
    and, under strict timing, that nothing is rounded -- with the reason when
    a time is off the sample clock. Given the line program (``digital``) and
    the setup, every other time that runs differently too: moves, settles,
    returns, line-step windows (from the realized schedule, §7.3)."""
    dwell = float(stage['sequence_time'])
    policy = scan_timing_policy(stage)
    realized, refusal = realized_duration(dwell, rate_hz, policy)
    others, problem = _other_timing_changes(stage, digital, setup, rate_hz)
    if policy == STRICT_POLICY:
        note = ('Strict timing (strict-v1): an order other than forward or an intra-pixel '
                'move is on, so every time must sit on the '
                f'{format_seconds(1 / float(rate_hz))} sample clock; nothing is rounded.')
        if refusal:
            return note + f' {refusal}.'
        return note + (f' {problem}' if problem else '')
    lines = []
    if realized != dwell:
        lines.append(f'Dwell {format_seconds(dwell)} runs as {format_seconds(realized)} '
                     f'(whole samples at {format_seconds(1 / float(rate_hz))}, {policy}).')
    return ' '.join(lines + others)


_STEP_PREFIX = re.compile(r'^step \d+, ')


def _sample_schedule(stage, digital, setup, rate_hz):
    """``(schedule, refusal)``: a copy of the scan two positions deep,
    realized -- every distinct time the whole scan realizes (line steps kept:
    each has its own windows), at a fraction of its size. ``(None, '')`` for
    a scan the program path does not describe (a swept one)."""
    if digital is None or setup is None:
        return None, ''
    try:
        program, context = program_from_advanced(stage, digital, setup)
    except (AdvancedConversionError, ProgramError, KeyError, TypeError, ValueError):
        return None, ''
    loops = tuple(loop if loop.kind == 'condition' else dataclasses.replace(
        loop, count=min(loop.count, 2)) for loop in program.loops)
    drives = tuple(drive if drive.table_um is None else dataclasses.replace(
        drive, table_um=tuple(drive.table_um[:2])) for drive in program.drives)
    try:
        return realize(expand(dataclasses.replace(program, loops=loops, drives=drives),
                              context), rate_hz), ''
    except ScheduleRefused as error:
        return None, str(error)
    except (InterpretError, PolicyError, ProgramError):
        return None, ''


def _distinct_changes(schedule):
    """Each change once, the step prefix dropped: ``[(label, change)]``."""
    out, seen = [], set()
    for change in schedule.changes:
        label = _STEP_PREFIX.sub('', change.where)
        key = (change.quantity, label, change.requested_s, change.realized_s)
        if key not in seen:
            seen.add(key)
            out.append((label, change))
    return out


def schedule_summary(stage, digital, setup, rate_hz):
    """What an Advanced scan's recording says about its timing (§7.9), or
    ``None`` for a swept scan: the policy with its contents, the grid, and
    each distinct time the scan realizes differently -- from the sample the
    timing note reads, so the file and the panel say the same."""
    schedule, _ = _sample_schedule(stage, digital, setup, rate_hz)
    if schedule is None:
        return None
    summary = schedule.summary()
    summary['changes'] = [dict(vars(change), where=label)
                          for label, change in _distinct_changes(schedule)]
    summary.pop('duration_s')          # the sample's, not the scan's
    summary.pop('error')
    return summary


def _other_timing_changes(stage, digital, setup, rate_hz) -> Tuple[List[str], str]:
    """``(lines, refusal)``: each distinct time other than the dwell that
    the schedule realizes differently, said once; or why it is refused."""
    schedule, refusal = _sample_schedule(stage, digital, setup, rate_hz)
    if schedule is None:
        return [], refusal
    lines = []
    for label, change in _distinct_changes(schedule):
        if change.quantity == 'step':
            continue           # the dwell, said above
        if change.quantity == 'event_edges':
            lines.append(f'{label} at {format_seconds(change.requested_s)} runs at '
                         f'{format_seconds(change.realized_s)}.')
        else:
            lines.append(f'{label[0].upper()}{label[1:]} {format_seconds(change.requested_s)} '
                         f'runs as {format_seconds(change.realized_s)}.')
    return lines, ''


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
    'AXIS_ORDER_KEY', 'AdvancedConversionError', 'COMPATIBLE_POLICY', 'FORMAT',
    'PROGRAM_DESIGNER', 'STRICT_POLICY', 'advanced_from_program', 'axis_order_refusal',
    'axis_visit_orders', 'interleave_permutation', 'parse_axis_order', 'program_scan_signals',
    'scan_timing_policy', 'timing_note', 'uses_program_features',
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

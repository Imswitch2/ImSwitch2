"""The old TriggerScope scan files as scan programs, and the old firmware's
own timeline to check them against.

``TriggerSwitch_0.1`` ran six fixed routines; ImSwitch saved their parameters
in ``imcontrol_scans`` as INI (``[scanParameterDict]``,
``[deviceParameterDict]``) for the pLS-RESOLFT widget and as INI or JSON
(``analogParameterDict``, ``digitalParameterDict``) for the raster widget.
``convert_legacy_scan`` turns such a file into a ``ScanProgram`` the Expert
panel runs on a step board; ``legacy_timeline`` is a line-by-line
transcription of the routine the old firmware ran for it; ``compare`` puts
the two side by side, frame by frame, so "the same scan" is checked, not
assumed. Every place where the model cannot say exactly what the old routine
did is a note of the conversion, never silent.

Supported: ``RASTER_SCAN`` (the raster widget) and
``pLS-RESOLFT-Multicolor_SCAN`` (the pLS-RESOLFT widget's multicolor mode,
which the one-colour files also use). The cycle's preamble -- the readout
device parked at its resting position, the activation and depletion pulses
fired there -- is the program's prologue of the plane loop.

Qt-free.
"""

from __future__ import annotations

import ast
import configparser
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .conformance import expected_log
from .interpreter import RunContext, expand
from .presets import exact_sum
from .program import (
    DetectorBinding, DeviceDrive, Loop, Motion, Prologue, ScanProgram, TimelineEvent, Wait,
    validate,
)
from .schedule import realize

STRICT = 'strict-v1'
RASTER = 'raster'
PLS_MULTICOLOR = 'pls-resolft-multicolor'

#: Loop kinds of the raster axes, by the last letter of the device's name.
_AXIS_KINDS = {'x': 'scan_x', 'y': 'scan_y', 'z': 'scan_z'}


class LegacyScanError(ValueError):
    """The file is not an old TriggerScope scan, or cannot be converted; the
    message says why."""


@dataclass
class LegacyScan:
    kind: str
    scan: Dict[str, object]
    devices: Dict[str, str]
    #: The raster widget's dicts, as saved.
    analog: Optional[Dict[str, list]] = None
    digital: Optional[Dict[str, list]] = None
    source: str = ''


@dataclass
class Conversion:
    program: ScanProgram
    notes: List[str] = field(default_factory=list)


# -- reading ------------------------------------------------------------------------

def _literal(text):
    text = text.strip()
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def read_legacy_scan(path) -> LegacyScan:
    """The scan a file holds, whichever of the old widgets saved it."""
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    if text.lstrip().startswith('{'):
        data = json.loads(text)
        if 'analogParameterDict' in data and 'digitalParameterDict' in data:
            return LegacyScan(RASTER, {}, {}, analog=dict(data['analogParameterDict']),
                              digital=dict(data['digitalParameterDict']), source=str(path))
        raise LegacyScanError(f'{path}: a JSON scan file without the raster widget\'s '
                              'analogParameterDict/digitalParameterDict.')
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise LegacyScanError(f'{path}: not an INI scan file ({error}).') from None
    sections = set(parser.sections())
    if {'scanParameterDict', 'deviceParameterDict'} <= sections:
        scan = {k: _literal(v) for k, v in parser['scanParameterDict'].items()}
        devices = {k: v.strip() for k, v in parser['deviceParameterDict'].items()}
        return LegacyScan(PLS_MULTICOLOR, scan, devices, source=str(path))
    if {'analogParameterDict', 'digitalParameterDict'} <= sections:
        analog = {k: _literal(v) for k, v in parser['analogParameterDict'].items()}
        digital = {k: _literal(v) for k, v in parser['digitalParameterDict'].items()}
        return LegacyScan(RASTER, {}, {}, analog=analog, digital=digital, source=str(path))
    raise LegacyScanError(f'{path}: sections {sorted(sections)} are not a TriggerScope '
                          'scan file (scanParameterDict/deviceParameterDict or '
                          'analogParameterDict/digitalParameterDict).')


# -- units --------------------------------------------------------------------------

def _seconds_from_ms(ms) -> float:
    """Milliseconds as typed, in seconds: the decimal, divided exactly, then
    the nearest float -- what typing the seconds would give."""
    return float(Decimal(repr(float(ms))) / Decimal(1000))


def _micros(ms) -> int:
    """What the old controller sent the firmware: ``int(ms * 1000)``."""
    return int(float(ms) * 1000)


def _whole(value, name) -> int:
    count = int(float(value))
    if count != float(value) or count < 1:
        raise LegacyScanError(f'{name} must be a whole number of at least 1, not {value!r}.')
    return count


def _pixels(length, step, name) -> int:
    """How many positions the old raster visited: ImSwitch trimmed the length
    by half a step so the firmware's inclusive end stopped at
    ``round(length / step)``."""
    if step <= 0:
        raise LegacyScanError(f'{name}: a scanned axis needs a positive step, not {step!r}.')
    return max(1, int(round(float(length) / float(step))))


# -- the pLS-RESOLFT multicolor routine ------------------------------------------------

@dataclass(frozen=True)
class _Multicolor:
    """The routine's parameters in the units the model wants: seconds and
    micrometres."""
    timepoints: int
    cycles: int
    planes: int
    cycle_start_um: float
    cycle_step_um: float
    ro_resting_um: float
    ro_start_um: float
    ro_step_um: float
    first_um: float
    second_um: float
    third_um: float
    delay_before_on_s: float
    on_s: float
    delay_after_on_s: float
    off_s: float
    delay_after_off_s: float
    delay_after_dac_step_s: float
    ro_s: float
    delay_after_ro_s: float
    laser2_s: float
    delay_after_laser2_s: float
    laser3_s: float
    delay_after_laser3_s: float
    timelapse_delay_s: float

    @classmethod
    def of(cls, scan: Mapping[str, object]):
        def num(key):
            try:
                return float(scan[key])
            except KeyError:
                raise LegacyScanError(f'The scan file lacks {key}.') from None
            except (TypeError, ValueError):
                raise LegacyScanError(f'{key} is not a number: {scan[key]!r}.') from None

        return cls(
            timepoints=_whole(num('timeLapsePoints'), 'timeLapsePoints'),
            cycles=_whole(num('cycleSteps'), 'cycleSteps'),
            planes=_whole(num('roSteps'), 'roSteps'),
            cycle_start_um=num('cycleStartPosUm'), cycle_step_um=num('cycleStepSizeUm'),
            ro_resting_um=num('roRestingPosUm'), ro_start_um=num('roStartPosUm'),
            ro_step_um=num('roStepSizeUm'),
            first_um=num('MulticolorScanFirstUm'), second_um=num('MulticolorScanSecondUm'),
            third_um=num('MulticolorScanThirdUm'),
            delay_before_on_s=_seconds_from_ms(num('delayBeforeOnTimeMs')),
            on_s=_seconds_from_ms(num('onTimeMs')),
            delay_after_on_s=_seconds_from_ms(num('delayAfterOnTimeMs')),
            off_s=_seconds_from_ms(num('offTimeMs')),
            delay_after_off_s=_seconds_from_ms(num('delayAfterOffTimeMs')),
            delay_after_dac_step_s=_seconds_from_ms(num('delayAfterDACStepMs')),
            ro_s=_seconds_from_ms(num('roTimeMs')),
            delay_after_ro_s=_seconds_from_ms(num('delayAfterRoMs')),
            laser2_s=_seconds_from_ms(num('Laser2OnMs')),
            delay_after_laser2_s=_seconds_from_ms(num('DelayAfterLaser2Ms')),
            laser3_s=_seconds_from_ms(num('Laser3OnMs')),
            delay_after_laser3_s=_seconds_from_ms(num('DelayAfterLaser3Ms')),
            timelapse_delay_s=float(scan.get('timeLapseDelayS', 0.0)),
        )


def _device(legacy: LegacyScan, key, device_map: Mapping[str, str]) -> str:
    try:
        name = legacy.devices[key]
    except KeyError:
        raise LegacyScanError(f'The scan file names no {key}.') from None
    return device_map.get(name, name)


def _convert_multicolor(legacy: LegacyScan, device_map: Mapping[str, str]) -> Conversion:
    p = _Multicolor.of(legacy.scan)
    notes = []
    galvo = _device(legacy, 'roScanDevice', device_map)
    galvox = _device(legacy, 'MulticolorScanDevice', device_map)
    ro = _device(legacy, 'roLaser', device_map)
    laser2 = _device(legacy, 'Laser2', device_map)
    laser3 = _device(legacy, 'Laser3', device_map)
    camera = _device(legacy, 'CameraTTL', device_map)
    cycle_device = legacy.devices.get('cycleScanDevice', '')
    if cycle_device:
        notes.append(f'The cycle offset is applied to {galvo}, not to {cycle_device}: the old '
                     'firmware added cycleStepSizeUm to the readout device and never moved '
                     f'{cycle_device} (its command is commented out).')

    # The step, as the old firmware ran it: the plane and the first colour
    # position are set as it begins, delayAfterDACStepMs passes, then the
    # readout pulse with the camera, laser 2 and laser 3 at the other two
    # positions of the multicolor device, and the readout pause. The move
    # to the next plane is the write at the step's end (a jump, no settle:
    # the settle is the delay at the next step's start).
    t_ro = p.delay_after_dac_step_s
    t_ro_end = exact_sum(t_ro, p.ro_s)
    t_l2_start = exact_sum(t_ro_end, p.delay_after_laser2_s)
    t_l2_end = exact_sum(t_l2_start, p.laser2_s)
    t_l3_start = exact_sum(t_l2_end, p.delay_after_laser3_s)
    t_cam_end = exact_sum(t_l3_start, p.laser3_s)
    step_s = exact_sum(t_cam_end, p.delay_after_ro_s)
    events = [TimelineEvent(ro, 'ttl', t_ro, t_ro_end, id='ro')]
    if p.laser2_s > 0:
        events.append(TimelineEvent(laser2, 'ttl', t_l2_start, t_l2_end, id='laser2'))
    else:
        notes.append(f'{laser2} is never on (Laser2OnMs = 0); its position and delay are kept.')
    if p.laser3_s > 0:
        events.append(TimelineEvent(laser3, 'ttl', t_l3_start, t_cam_end, id='laser3'))
    else:
        notes.append(f'{laser3} is never on (Laser3OnMs = 0); its position and delay are kept.')
    # One exposure over the three colours: the camera stays open while the
    # multicolor device hops, which is the point of the scan.
    events.append(TimelineEvent(camera, 'detector', t_ro, t_cam_end, during='any',
                                id='exposure'))
    if t_l2_end > t_ro_end:
        events.append(TimelineEvent(galvox, 'offset', t_ro_end, t_l2_end,
                                    value=p.second_um - p.first_um, during='any',
                                    id='multicolor-second'))
    if step_s > t_l2_end:
        events.append(TimelineEvent(galvox, 'offset', t_l2_end, step_s,
                                    value=p.third_um - p.first_um, during='any',
                                    id='multicolor-third'))
    notes.append(f'{galvox} stands at its third position until the next step begins, as the '
                 'old firmware left it; after the scan it returns to the first position '
                 f'({p.first_um:g} µm), where the old firmware left it at the third.')

    # Before every cycle the old firmware parked the readout device at
    # roRestingPosUm, waited delayBeforeOnTimeMs (not before the first
    # cycle), fired the activation pulse, waited, fired the depletion pulse,
    # waited, then set the first plane and waited delayAfterDACStepMs. That
    # is the prologue of the plane loop: the device stands roRestingPosUm -
    # roStartPosUm from the pass's first position, the pulses fire in it,
    # and its end holds the move to the first plane with its settle.
    on_laser = _device(legacy, 'onLaser', device_map)
    off_laser = _device(legacy, 'offLaser', device_map)
    t_on = p.delay_before_on_s
    t_on_end = exact_sum(t_on, p.on_s)
    t_off = exact_sum(t_on_end, p.delay_after_on_s)
    t_off_end = exact_sum(t_off, p.off_s)
    prologue_s = exact_sum(t_off_end, p.delay_after_off_s)
    if prologue_s <= 0:
        raise LegacyScanError('The cycle preamble has no duration at all (every delay and '
                              'pulse 0 ms); the old firmware still wrote the resting '
                              'position for a few microseconds, which no program says.')
    if p.on_s > 0:
        events.append(TimelineEvent(on_laser, 'ttl', t_on, t_on_end, anchor='prologue:plane',
                                    during='any', id='activation'))
    else:
        notes.append(f'{on_laser} is never on (onTimeMs = 0); its delays are kept.')
    if p.off_s > 0:
        events.append(TimelineEvent(off_laser, 'ttl', t_off, t_off_end,
                                    anchor='prologue:plane', during='any', id='depletion'))
    else:
        notes.append(f'{off_laser} is never on (offTimeMs = 0); its delays are kept.')
    prologues = (Prologue('plane', prologue_s,
                          offsets_um={galvo: p.ro_resting_um - p.ro_start_um}),)
    if p.delay_before_on_s > 0:
        notes.append(f'Before the first cycle of a time point the old firmware skipped the '
                     f'{p.delay_before_on_s * 1e3:g} ms delayBeforeOnTimeMs; the program waits '
                     'it before every cycle.')
    notes.append(f'During the cycle preamble {galvox} stands at its first position; the old '
                 'firmware left it at its third from the previous plane (no laser is on then).')

    motion = Motion(transition='jump', move_s=0.0, settle_s=0.0, placement='in-step',
                    return_s=0.0, origin='absolute', restore='park')
    still = Motion(transition='jump', move_s=0.0, settle_s=0.0, placement='in-step',
                   return_s=0.0, origin='absolute', restore='first')
    loops = []
    if p.timepoints > 1:
        loops.append(Loop('time', 'time', p.timepoints))
    loops += [Loop('cycle', 'cycle', p.cycles), Loop('plane', 'plane', p.planes)]
    weights = {'cycle': p.cycle_step_um, 'plane': p.ro_step_um}
    waits = ()
    if p.timepoints > 1 and p.timelapse_delay_s > 0:
        waits = (Wait(at='time', trigger='time', interval_s=p.timelapse_delay_s),)
        notes.append(f'The time-lapse delay of {p.timelapse_delay_s:g} s is waited before every '
                     'time point, the first included; the old firmware waited it after every '
                     'time point but the last.')
    program = ScanProgram(
        loops=tuple(loops),
        drives=(DeviceDrive(galvo, offset_um=exact_sum(p.cycle_start_um, p.ro_start_um),
                            weights_um=weights, motion=motion),
                DeviceDrive(galvox, offset_um=p.first_um, motion=still)),
        step_s=step_s, timeline=tuple(events), waits=waits,
        detectors=(DetectorBinding(camera, events='exposure'),),
        prologues=prologues,
        park={galvo: p.ro_resting_um},
        timing_policy=STRICT,
        requested_as={'format': 'triggerscope-pls-multicolor/1',
                      'scanParameterDict': dict(legacy.scan),
                      'deviceParameterDict': dict(legacy.devices),
                      'source': legacy.source},
    )
    return Conversion(validate(program), notes)


def _multicolor_timeline(legacy: LegacyScan, device_map: Mapping[str, str]):
    """``pLS-RESOLFT-Multicolor_SCAN`` as the old firmware ran it, in integer
    microseconds from its start: ``{'dac': [(t, device, µm)], 'on': [(start,
    end, device)]}``. Positions are in µm (the firmware worked in volts; the
    conversion is linear, so the µm are what it meant)."""
    p = _Multicolor.of(legacy.scan)
    s = legacy.scan
    galvo = _device(legacy, 'roScanDevice', device_map)
    galvox = _device(legacy, 'MulticolorScanDevice', device_map)
    on_laser = _device(legacy, 'onLaser', device_map)
    off_laser = _device(legacy, 'offLaser', device_map)
    ro = _device(legacy, 'roLaser', device_map)
    laser2 = _device(legacy, 'Laser2', device_map)
    laser3 = _device(legacy, 'Laser3', device_map)
    camera = _device(legacy, 'CameraTTL', device_map)
    us = {k: _micros(s[k]) for k in (
        'delayBeforeOnTimeMs', 'onTimeMs', 'delayAfterOnTimeMs', 'offTimeMs',
        'delayAfterOffTimeMs', 'delayAfterDACStepMs', 'roTimeMs', 'delayAfterRoMs',
        'Laser2OnMs', 'DelayAfterLaser2Ms', 'Laser3OnMs', 'DelayAfterLaser3Ms')}
    timelapse_us = int(float(s.get('timeLapseDelayS', 0.0)) * 1_000_000)

    dac, on, high = [], [], {}
    t = 0

    def write(device, um):
        dac.append((t, device, float(um)))

    def ttl(device, level):
        if level:
            high.setdefault(device, t)
        elif device in high:
            start = high.pop(device)
            if t > start:
                on.append((start, t, device))

    for tp in range(p.timepoints):
        for k in range(p.cycles):
            base = p.cycle_start_um + k * p.cycle_step_um
            write(galvo, base + p.ro_resting_um)
            if k != 0:
                t += us['delayBeforeOnTimeMs']
            ttl(camera, 0)
            ttl(on_laser, 1)
            t += us['onTimeMs']
            ttl(on_laser, 0)
            t += us['delayAfterOnTimeMs']
            ttl(off_laser, 1)
            t += us['offTimeMs']
            ttl(off_laser, 0)
            t += us['delayAfterOffTimeMs']
            for i in range(p.planes):
                write(galvo, base + p.ro_start_um + i * p.ro_step_um)
                write(galvox, p.first_um)
                t += us['delayAfterDACStepMs']
                ttl(ro, 1)
                ttl(camera, 1)
                t += us['roTimeMs']
                ttl(ro, 0)
                write(galvox, p.second_um)
                t += us['DelayAfterLaser2Ms']
                ttl(laser2, 1)
                t += us['Laser2OnMs']
                ttl(laser2, 0)
                write(galvox, p.third_um)
                t += us['DelayAfterLaser3Ms']
                ttl(laser3, 1)
                t += us['Laser3OnMs']
                ttl(laser3, 0)
                ttl(camera, 0)
                t += us['delayAfterRoMs']
        if tp < p.timepoints - 1:
            t += timelapse_us
    write(galvo, p.ro_resting_um)
    return {'dac': dac, 'on': sorted(on), 'camera': camera, 'end': t}


# -- the raster routine --------------------------------------------------------------

@dataclass(frozen=True)
class _Axis:
    device: str
    start_um: float
    step_um: float
    pixels: int


def _raster_axes(legacy: LegacyScan, device_map) -> Tuple[List[_Axis], List[str]]:
    """The axes the old raster scanned (dims one to four, a length above
    zero), innermost first, and notes on the dims it only parked."""
    analog = legacy.analog or {}
    try:
        targets = list(analog['target_device'])
        lengths = [float(v) for v in analog['axis_length']]
        steps = [float(v) for v in analog['axis_step_size']]
        starts = [float(v) for v in analog['axis_startpos']]
    except (KeyError, TypeError, ValueError) as error:
        raise LegacyScanError(f'The raster file\'s analogParameterDict is incomplete: {error}.')
    axes, notes = [], []
    for index, (target, length, step, start) in enumerate(zip(targets, lengths, steps, starts)):
        name = device_map.get(target, target)
        if index >= 4:
            if length:
                notes.append(f'{name} (dimension {index + 1}) was never sent to the old '
                             'firmware, which takes four dimensions; it is left out.')
            continue
        if length > 0:
            axes.append(_Axis(name, start, step, _pixels(length, step, name)))
        else:
            notes.append(f'The old firmware set {name} to {start:g} µm for the scan (a '
                         'dimension of length 0); the program leaves it where it is.')
    if not axes:
        raise LegacyScanError('No axis has a length above zero: nothing to scan.')
    return axes, notes


def _raster_rows(legacy: LegacyScan, device_map) -> Tuple[List[Tuple[str, float, float]], float]:
    digital = legacy.digital or {}
    try:
        devices = list(digital['target_device'])
        starts = [float(v) for v in digital['TTL_start']]
        ends = [float(v) for v in digital['TTL_end']]
        dwell = float(digital['sequence_time'])
    except (KeyError, TypeError, ValueError) as error:
        raise LegacyScanError(f'The raster file\'s digitalParameterDict is incomplete: {error}.')
    rows = [(device_map.get(d, d), s, e) for d, s, e in zip(devices, starts, ends)]
    if dwell <= 0:
        raise LegacyScanError(f'sequence_time must be positive, not {dwell!r}.')
    return rows, dwell


def _convert_raster(legacy: LegacyScan, device_map, camera: Optional[str]) -> Conversion:
    axes, notes = _raster_axes(legacy, device_map)
    rows, dwell = _raster_rows(legacy, device_map)
    if not rows:
        raise LegacyScanError('The raster has no TTL row: nothing pulses.')
    # The old firmware ignored every row but the earliest, and pulsed TTL
    # lines 0 to 3 together in that window ("Temporary fix" in
    # runPixelCycle); the program fires each row on its own line.
    earliest = min(rows, key=lambda row: row[1])
    for device, start, end in rows:
        if end <= start:
            raise LegacyScanError(f'{device}: TTL_end must be after TTL_start.')
        if end > dwell:
            raise LegacyScanError(f'{device}: its window ends at {end:g} s, after the pixel '
                                  f'({dwell:g} s).')
    if len(rows) > 1:
        notes.append('The old firmware pulsed TTL lines 0-3 together in the earliest row\'s '
                     f'window ({earliest[0]}, {earliest[1]:g}-{earliest[2]:g} s) and ignored '
                     'the other rows; the program fires every row in its own window.')
    else:
        notes.append('The old firmware pulsed TTL lines 0-3 together in this window, whatever '
                     'was on them; the program fires only the device named.')
    events = []
    for index, (device, start, end) in enumerate(rows, start=1):
        events.append(TimelineEvent(device, 'ttl', float(start), float(end), id=f'row{index}'))
    detectors = ()
    if camera:
        events.append(TimelineEvent(camera, 'detector', float(earliest[1]), float(earliest[2]),
                                    id='exposure'))
        detectors = (DetectorBinding(camera, events='exposure'),)
        notes.append(f'{camera} is triggered in the earliest row\'s window, as its line '
                     '(TTL3, among lines 0-3) was by the old firmware.')
    else:
        notes.append('No camera is bound: the old firmware pulsed TTL3 with lines 0-2 every '
                     'pixel, so a camera on it was triggered; name one with --camera to keep '
                     'that.')
    motion = Motion(transition='jump', move_s=0.0, settle_s=0.0, placement='in-step',
                    return_s=0.0, origin='absolute', restore='first')
    loops, drives = [], []
    for axis in reversed(axes):
        kind = _AXIS_KINDS.get(axis.device.strip().lower()[-1:], 'position')
        loop_id = axis.device.lower().replace(' ', '_')
        loops.append(Loop(loop_id, kind, axis.pixels))
        drives.append(DeviceDrive(axis.device, offset_um=axis.start_um,
                                  weights_um={loop_id: axis.step_um}, motion=motion))
    program = ScanProgram(
        loops=tuple(loops), drives=tuple(drives), step_s=dwell, timeline=tuple(events),
        detectors=detectors, timing_policy=STRICT,
        requested_as={'format': 'triggerscope-raster/1',
                      'analogParameterDict': dict(legacy.analog or {}),
                      'digitalParameterDict': dict(legacy.digital or {}),
                      'source': legacy.source},
    )
    return Conversion(validate(program), notes)


def _raster_timeline(legacy: LegacyScan, device_map, camera: Optional[str]):
    """``RASTER_SCAN`` as the old firmware ran it, with ImSwitch's trimmed
    lengths: ``{'dac', 'on'}`` in integer microseconds. Only the devices the
    program names are listed among the lines 0-3 the firmware pulsed."""
    axes, _ = _raster_axes(legacy, device_map)
    rows, dwell = _raster_rows(legacy, device_map)
    earliest = min(rows, key=lambda row: row[1])
    start_us, end_us, dwell_us = (int(round(earliest[1] * 1e6)), int(round(earliest[2] * 1e6)),
                                  int(round(dwell * 1e6)))
    pulsed = [earliest[0]] + ([camera] if camera and camera != earliest[0] else [])
    dac, on = [], []
    t = 0
    counts = [axis.pixels for axis in axes]      # innermost first

    def positions(index):
        remaining, values = index, []
        for count in counts:
            values.append(remaining % count)
            remaining //= count
        return values

    total = 1
    for count in counts:
        total *= count
    for index in range(total):
        for axis, i in zip(axes, positions(index)):
            dac.append((t, axis.device, axis.start_um + i * axis.step_um))
        for device in pulsed:
            on.append((t + start_us, t + end_us, device))
        t += dwell_us
    for axis in axes:
        dac.append((t, axis.device, axis.start_um))
    return {'dac': dac, 'on': sorted(on), 'camera': camera, 'end': t}


# -- the public conversion -------------------------------------------------------------

def convert_legacy_scan(legacy: LegacyScan, *, device_map: Optional[Mapping[str, str]] = None,
                        camera: Optional[str] = None) -> Conversion:
    """The old scan as a program. ``device_map`` renames devices (``{'Orca':
    'OrcaFusion'}``); ``camera`` binds a camera to a raster, which named none."""
    device_map = dict(device_map or {})
    if legacy.kind == PLS_MULTICOLOR:
        return _convert_multicolor(legacy, device_map)
    if legacy.kind == RASTER:
        return _convert_raster(legacy, device_map, camera)
    raise LegacyScanError(f'Unknown scan kind {legacy.kind!r}.')


def legacy_timeline(legacy: LegacyScan, *, device_map: Optional[Mapping[str, str]] = None,
                    camera: Optional[str] = None):
    """What the old firmware did for this scan, in microseconds (see the
    module docstring); the oracle a conversion is checked against."""
    device_map = dict(device_map or {})
    if legacy.kind == PLS_MULTICOLOR:
        return _multicolor_timeline(legacy, device_map)
    if legacy.kind == RASTER:
        return _raster_timeline(legacy, device_map, camera)
    raise LegacyScanError(f'Unknown scan kind {legacy.kind!r}.')


# -- the comparison ---------------------------------------------------------------------

def _frames(dac, on, frame_device, per_segment=None):
    """Frames from a timeline, one per on-interval of ``frame_device``, in
    segments of ``per_segment`` frames (a time point; all in one without).
    A frame is (positions of every device written so far, every other pulse
    relative to the frame start, the frame start relative to the first frame
    of its segment)."""
    windows = sorted(w for w in on if w[2] == frame_device)
    others = sorted(w for w in on if w[2] != frame_device)
    writes = sorted(dac)
    segments, frames, first = [], [], None
    for number, (start, end, _) in enumerate(windows):
        if per_segment and number and number % per_segment == 0:
            segments.append(frames)
            frames, first = [], None
        if first is None:
            first = start
        held = {}
        for t, device, value in writes:
            if t <= start:
                held[device] = value
        pulses = tuple(sorted((d, s - start, e - start) for s, e, d in others
                              if s < end and e > start))
        positions = tuple(sorted((d, round(v, 9)) for d, v in held.items()))
        frames.append((positions, pulses, start - first))
    if frames:
        segments.append(frames)
    return segments


def _dark_pulses(on, frame_device, per_segment=None):
    """Per segment, every pulse outside the frames (the cycle preamble's
    activation and depletion), relative to the segment's first frame."""
    windows = sorted(w for w in on if w[2] == frame_device)
    others = sorted(w for w in on if w[2] != frame_device)
    per = per_segment or len(windows) or 1
    segments = []
    for number in range(0, len(windows), per):
        chunk = windows[number:number + per]
        lo = chunk[0][0]
        hi = chunk[-1][1]
        before = windows[number - 1][1] if number else -1
        dark = tuple(sorted((d, s - lo, e - lo) for s, e, d in others
                            if s >= before and e <= hi + 1
                            and not any(s < fe and e > fs for fs, fe, _ in chunk)))
        segments.append(dark)
    return segments


def _frames_per_timepoint(legacy: LegacyScan) -> Optional[int]:
    if legacy.kind != PLS_MULTICOLOR:
        return None
    p = _Multicolor.of(legacy.scan)
    return p.cycles * p.planes if p.timepoints > 1 else None


def compare(legacy: LegacyScan, conversion: Conversion, *, device_map=None, camera=None,
            rate_hz: float = 1e6) -> List[str]:
    """Every difference between the old firmware's frames and the program's:
    per frame, the positions of the devices written so far, every pulse
    relative to the frame start, and the frame's start relative to the first
    frame of its time point. Empty when the program means the old scan."""
    device_map = dict(device_map or {})
    old = legacy_timeline(legacy, device_map=device_map, camera=camera)
    program = conversion.program
    schedule = realize(expand(program, RunContext()), rate_hz)
    log = expected_log(schedule)
    to_us = 1e6 / rate_hz
    frame_device = old['camera']
    if frame_device is None:
        frame_device = min(program.timeline, key=lambda e: e.start_s).device
    per_segment = _frames_per_timepoint(legacy)
    old_segments = _frames(old['dac'], old['on'], frame_device, per_segment)
    # The program's log restarts its ticks at every barrier that waits for a
    # trigger; these programs have none, so the ticks run through.
    if len({s for s, *_ in log['on']}) > 1:
        return ['The program has more than one log segment; the comparison expects a '
                'program without trigger waits.']
    new_dac = [(int(round(t * to_us)), d, v) for _, t, d, v in log['dac']]
    # An offset event is what the old firmware did as two DAC writes: the
    # device's position plus the offset when it starts, the position again
    # when it ends.
    for event in schedule.events:
        if event.kind != 'offset':
            continue
        base = [v for t, d, v in sorted(new_dac) if d == event.device
                and t <= int(round(event.start_tick * to_us))]
        base = base[-1] if base else 0.0
        new_dac.append((int(round(event.start_tick * to_us)), event.device,
                        base + float(event.value)))
        new_dac.append((int(round(event.end_tick * to_us)), event.device, base))
    new_on = [(int(round(e.start_tick * to_us)), int(round(e.end_tick * to_us)), e.device)
              for e in schedule.events if e.kind in ('ttl', 'detector')]
    new_segments = _frames(new_dac, new_on, frame_device, per_segment)
    problems = []
    if len(old_segments) != len(new_segments):
        problems.append(f'{len(old_segments)} time points in the old scan, '
                        f'{len(new_segments)} in the program.')
    old_dark = _dark_pulses(old['on'], frame_device, per_segment)
    new_dark = _dark_pulses(new_on, frame_device, per_segment)
    for index, (a, b) in enumerate(zip(old_dark, new_dark)):
        if a != b:
            problems.append(f'time point {index}: pulses outside the frames {a} in the old '
                            f'scan, {b} in the program.')
    for index, (old_frames, new_frames) in enumerate(zip(old_segments, new_segments)):
        if len(old_frames) != len(new_frames):
            problems.append(f'time point {index}: {len(old_frames)} frames in the old scan, '
                            f'{len(new_frames)} in the program.')
        for number, (a, b) in enumerate(zip(old_frames, new_frames)):
            if a[0] != b[0]:
                problems.append(f'time point {index}, frame {number}: positions {a[0]} in the '
                                f'old scan, {b[0]} in the program.')
            if a[1] != b[1]:
                problems.append(f'time point {index}, frame {number}: pulses {a[1]} in the old '
                                f'scan, {b[1]} in the program.')
            if a[2] != b[2]:
                problems.append(f'time point {index}, frame {number}: starts {a[2]} µs after '
                                f'the first frame in the old scan, {b[2]} µs in the program.')
            if len(problems) > 20:
                problems.append('...')
                return problems
    return problems


__all__ = ['Conversion', 'LegacyScan', 'LegacyScanError', 'PLS_MULTICOLOR', 'RASTER',
           'compare', 'convert_legacy_scan', 'legacy_timeline', 'read_legacy_scan']


# Copyright (C) 2026 ImSwitch developers
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

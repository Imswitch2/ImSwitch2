"""Expert presets: programs made from a few parameters
(docs/design/plans/scan-engines-and-expert-scan.md §2.3, §4, M3).

A preset is a function from its parameters to a program, and a description
of those parameters for a form. Every value is a parameter: nothing here
defaults a time or a distance. The parameters a program was made from are
kept in its ``requested_as``, so the panel can open the form again with them.

- ``ms-resolft``: Snouty's multi-scan RESOLFT, *cycles* x *planes* on one
  readout device, positions visited cycle by cycle (1, 3, 5, 2, 4, 6 for two
  cycles of three planes), a pulse sequence at every position, the camera
  exposing during the readout pulse, *timepoints* times.
- ``pulse-sequence``: a pulse sequence repeated *frames* times, no positions
  -- widefield RESOLFT, or anything a pulse generator plays alone.
- ``raster``: a stepped X/Y raster with a pulse sequence at every pixel.

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Tuple

from .program import (
    DetectorBinding, DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait, validate,
)

STRICT = 'strict-v1'


@dataclass(frozen=True)
class Field:
    """One preset parameter. ``kind``: 'positioner', 'laser', 'detector'
    (a device of the setup), 'count', 'seconds', 'micrometres', 'choice' or
    'pulses' (a list of ``[device, on_s, gap_after_s]``)."""
    name: str
    label: str
    kind: str
    choices: Tuple[str, ...] = ()
    help: str = ''


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    fields: Tuple[Field, ...]
    build: Callable[..., ScanProgram]
    summary: str = ''


class PresetError(ValueError):
    """A preset's parameters do not make a program; the message says which."""


def _timeline(pulses, camera, exposure_pulse) -> Tuple[List[TimelineEvent], float]:
    """Pulses one after another from 0, and the camera during pulse number
    ``exposure_pulse`` (1-based). Returns the events and where they end."""
    if not pulses:
        raise PresetError('A pulse sequence needs at least one pulse.')
    events, t, window = [], 0.0, None
    for index, (device, on_s, gap_s) in enumerate(pulses, start=1):
        on_s, gap_s = float(on_s), float(gap_s)
        if on_s <= 0 or gap_s < 0:
            raise PresetError(f'Pulse {index} ({device}): on must be positive and the gap '
                              'after it not negative.')
        events.append(TimelineEvent(str(device), 'ttl', t, t + on_s, id=f'pulse{index}'))
        if index == exposure_pulse:
            window = (t, t + on_s)
        t += on_s + gap_s
    if camera:
        if window is None:
            raise PresetError(f'The camera exposes during pulse {exposure_pulse}; the sequence '
                              f'has {len(pulses)}.')
        events.append(TimelineEvent(str(camera), 'detector', *window, id='exposure'))
    return events, t


def _count(value, name, minimum=1):
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise PresetError(f'{name} must be a whole number, not {value!r}.') from None
    if count < minimum or count != float(value):
        raise PresetError(f'{name} must be a whole number of at least {minimum}.')
    return count


def ms_resolft(*, device, cycles, planes, cycle_step_um, start_um, pulses, camera,
               exposure_pulse, timepoints, pause_s, move_s, settle_s,
               transition='jump') -> ScanProgram:
    """Snouty's MS-RESOLFT: position = start + dy * (cycle + cycles * plane),
    visited cycle by cycle, ``timepoints`` times, a pause before each."""
    cycles = _count(cycles, 'Cycles')
    planes = _count(planes, 'Planes')
    timepoints = _count(timepoints, 'Timepoints')
    exposure_pulse = _count(exposure_pulse, 'Exposure pulse')
    dy = float(cycle_step_um)
    move_s, settle_s, pause_s = float(move_s), float(settle_s), float(pause_s)
    events, length = _timeline(pulses, camera, exposure_pulse)
    motion = Motion(transition=transition, move_s=move_s, settle_s=settle_s,
                    placement='in-step', return_s=move_s + settle_s,
                    origin='absolute', restore='first')
    program = ScanProgram(
        loops=(Loop('time', 'time', timepoints), Loop('cycle', 'cycle', cycles),
               Loop('plane', 'plane', planes)),
        drives=(DeviceDrive(str(device), offset_um=float(start_um),
                            weights_um={'cycle': dy, 'plane': cycles * dy}, motion=motion),),
        step_s=length + move_s + settle_s,
        timeline=tuple(events),
        waits=(Wait(at='time', trigger='time', interval_s=pause_s),) if pause_s > 0 else (),
        detectors=(DetectorBinding(str(camera), events='exposure'),) if camera else (),
        timing_policy=STRICT,
    )
    return validate(program)


def pulse_sequence(*, pulses, camera, exposure_pulse, frames, pause_s) -> ScanProgram:
    """A pulse sequence ``frames`` times, a pause before each frame."""
    frames = _count(frames, 'Frames')
    events, length = _timeline(pulses, camera, _count(exposure_pulse, 'Exposure pulse'))
    pause_s = float(pause_s)
    return validate(ScanProgram(
        loops=(Loop('frame', 'time', frames),), drives=(), step_s=length,
        timeline=tuple(events),
        waits=(Wait(at='frame', trigger='time', interval_s=pause_s),) if pause_s > 0 else (),
        detectors=(DetectorBinding(str(camera), events='exposure'),) if camera else (),
        timing_policy=STRICT,
    ))


def raster(*, x_device, y_device, x_pixels, y_pixels, x_step_um, y_step_um, x_start_um,
           y_start_um, pulses, camera, exposure_pulse, move_s, settle_s, return_s,
           traversal='forward') -> ScanProgram:
    """A stepped raster, X fast; serpentine runs every other line back."""
    nx, ny = _count(x_pixels, 'X pixels'), _count(y_pixels, 'Y pixels')
    events, length = _timeline(pulses, camera or None, _count(exposure_pulse, 'Exposure pulse'))
    move_s, settle_s = float(move_s), float(settle_s)
    motion = Motion(transition='smooth', move_s=move_s, settle_s=settle_s, placement='in-step',
                    return_s=float(return_s), origin='absolute', restore='first')
    x_loop = Loop('x', 'scan_x', nx) if traversal == 'forward' else \
        Loop('x', 'scan_x', nx, traversal='serpentine', parity_loops=('y',))
    return validate(ScanProgram(
        loops=(Loop('y', 'scan_y', ny), x_loop),
        drives=(DeviceDrive(str(x_device), offset_um=float(x_start_um),
                            weights_um={'x': float(x_step_um)}, motion=motion),
                DeviceDrive(str(y_device), offset_um=float(y_start_um),
                            weights_um={'y': float(y_step_um)}, motion=motion)),
        step_s=length + move_s + settle_s, timeline=tuple(events),
        detectors=(DetectorBinding(str(camera), events='exposure'),) if camera else (),
        timing_policy=STRICT,
    ))


_PULSES = Field('pulses', 'Pulse sequence', 'pulses',
                help='Lasers one after another: on time, then a gap before the next.')
_CAMERA = Field('camera', 'Camera', 'detector', help='Leave empty for no camera.')
_EXPOSURE = Field('exposure_pulse', 'Camera exposes during pulse', 'count',
                  help='The number of the pulse the camera exposes with (1 = the first).')
_MOVE = Field('move_s', 'Move time', 'seconds',
              help='How long the device takes from one position to the next.')
_SETTLE = Field('settle_s', 'Settle time', 'seconds',
                help='How long it rests at a new position before the pulses start.')

PRESETS: Dict[str, Preset] = {
    'ms-resolft': Preset(
        'ms-resolft', 'MS-RESOLFT (cycles x planes)',
        (Field('device', 'Readout device', 'positioner'),
         Field('cycles', 'Cycles', 'count'),
         Field('planes', 'Planes per cycle', 'count'),
         Field('cycle_step_um', 'Cycle step', 'micrometres',
               help='Between neighbouring positions; planes are cycles x this apart.'),
         Field('start_um', 'First position', 'micrometres'),
         _PULSES, _CAMERA, _EXPOSURE,
         Field('timepoints', 'Timepoints', 'count'),
         Field('pause_s', 'Pause before each timepoint', 'seconds'),
         _MOVE, _SETTLE,
         Field('transition', 'Move shape', 'choice', ('jump', 'linear', 'smooth'))),
        ms_resolft,
        'Positions start + dy * (cycle + cycles * plane), cycle by cycle; the camera '
        'layout carries cycle and plane, which the SNOUTY reconstructor reads.'),
    'pulse-sequence': Preset(
        'pulse-sequence', 'Pulse sequence (widefield RESOLFT)',
        (_PULSES, _CAMERA, _EXPOSURE, Field('frames', 'Frames', 'count'),
         Field('pause_s', 'Pause before each frame', 'seconds')),
        pulse_sequence,
        'Lasers and camera only: a pulse generator can play it alone.'),
    'raster': Preset(
        'raster', 'Stepped raster',
        (Field('x_device', 'X device', 'positioner'), Field('y_device', 'Y device', 'positioner'),
         Field('x_pixels', 'X pixels', 'count'), Field('y_pixels', 'Y pixels', 'count'),
         Field('x_step_um', 'X step', 'micrometres'), Field('y_step_um', 'Y step', 'micrometres'),
         Field('x_start_um', 'X first position', 'micrometres'),
         Field('y_start_um', 'Y first position', 'micrometres'),
         _PULSES, _CAMERA, _EXPOSURE, _MOVE, _SETTLE,
         Field('return_s', 'Line return time', 'seconds'),
         Field('traversal', 'X order', 'choice', ('forward', 'serpentine'))),
        raster,
        'A pulse sequence at every pixel; APDs and PMTs assemble it as an image.'),
}


def make(preset_id: str, params: Mapping[str, object]) -> ScanProgram:
    """The program a preset makes from ``params``, remembering them."""
    try:
        preset = PRESETS[preset_id]
    except KeyError:
        raise PresetError(f'No preset {preset_id!r}; there are {", ".join(PRESETS)}.') from None
    # A camera is optional; every other field must be filled in.
    missing = [f.label for f in preset.fields if f.kind != 'detector'
               and (f.name not in params or params[f.name] in (None, ''))]
    if missing:
        raise PresetError(f'{preset.title}: fill in {", ".join(missing)}.')
    program = preset.build(**{f.name: params.get(f.name) for f in preset.fields})
    return _with_request(program, preset_id, params)


def _with_request(program, preset_id, params):
    from dataclasses import replace
    return replace(program, requested_as={'format': f'preset:{preset_id}/1',
                                          'params': _json_params(params)})


def _json_params(params):
    out = {}
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            out[key] = [list(v) if isinstance(v, (list, tuple)) else v for v in value]
        else:
            out[key] = value
    return out


def preset_of(program) -> Tuple[str, Dict[str, object]]:
    """``(preset id, params)`` a program was made from, or ``('', {})``."""
    request = getattr(program, 'requested_as', None) or {}
    fmt = str(request.get('format', ''))
    if fmt.startswith('preset:') and fmt.endswith('/1'):
        return fmt[len('preset:'):-len('/1')], dict(request.get('params') or {})
    return '', {}


__all__ = ['Field', 'PRESETS', 'Preset', 'PresetError', 'make', 'ms_resolft', 'preset_of',
           'pulse_sequence', 'raster']


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

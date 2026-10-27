"""Copy the current acquisition as Python.

``acquisitionScript(api.imcontrol)`` reads what ImControl is set to --
through the scripting API only, so the script it writes uses nothing a user
could not -- and returns a script that sets it all again and acquires:

* the detectors the Recording widget records, with every editable setting
  of each (exposure, trigger source, ...) -- not the calibrations the setup
  file owns (the camera pixel size);
* every laser's value and whether it is on;
* the scan the Scan widget holds, as the dict of ``getScanParameters()``;
* where the stages and rotators are -- written down, but moved there only
  when the user sets ``MOVE_TO_POSITIONS`` (putting a stage back is a
  decision of its own, not part of reproducing settings);
* the acquisition the Recording widget is set to: the scan once into memory
  for a scan mode, otherwise its number of frames into memory.

The script is cell-structured (``# %%``): settings, apply, acquire. Only the
widgets the setup loads contribute; a setup without lasers gets no laser
cell. Values are written as Python literals and checked to read back the
same; a value that does not (an object no literal can spell) is left out,
with a comment naming it.
"""

import ast
import pprint
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

#: Recording modes that acquire through the scan.
_SCAN_MODES = ('ScanOnce', 'ScanLapse')
_WIDTH = 79


def acquisitionScript(imcontrol, *, setupName: Optional[str] = None,
                      now: Optional[datetime] = None) -> str:
    """The current acquisition of ``imcontrol`` (``api.imcontrol``) as a
    runnable script."""
    now = now or datetime.now()
    has = lambda name: _has(imcontrol, name)  # noqa: E731
    skipped: List[str] = []

    recording = imcontrol.getRecordingSettings() if has('getRecordingSettings') else {}
    detectors = list(recording.get('detectors') or imcontrol.getDetectorNames()[:1])
    settings = {d: _detectorSettings(imcontrol, d, skipped) for d in detectors}
    lasers = _lasers(imcontrol, skipped) if has('getLaserNames') else {}
    scan = _literal(imcontrol.getScanParameters(), 'the scan parameters', skipped) \
        if has('getScanParameters') else None
    stages = _literal(imcontrol.getPositionerPositions(), 'the stage positions', skipped) \
        if has('getPositionerPositions') else {}
    rotators = {name: float(imcontrol.getRotatorPosition(name))
                for name in imcontrol.getRotatorNames()} if has('getRotatorNames') else {}
    if scan is _SKIP:
        scan = None
    if stages is _SKIP:
        stages = {}
    useScan = scan is not None and recording.get('mode') in _SCAN_MODES
    frames = int(recording.get('frames') or 1)

    out: List[str] = []
    out += _header(now, setupName, useScan, frames)
    out += ['# %% What was set', '']
    out += [f'DETECTORS = {_fmt(detectors, "DETECTORS = ")}', '']
    out += ['# Every editable setting of each detector, as the Settings widget showed',
            '# it (units in the comments).',
            'DETECTOR_SETTINGS = {']
    for detector in detectors:
        out.append(f'    {detector!r}: {{')
        for name, (value, units) in settings[detector].items():
            line = f'        {name!r}: {value!r},'
            out.append(line + (f'  # {units}' if units else ''))
        out.append('    },')
    out += ['}', '']
    if lasers:
        out += ["# Every laser: switched on or not, and its value in the laser's units",
                '# (an on/off laser has no value).',
                'LASERS = {']
        for name, (state, comment) in lasers.items():
            out.append(f'    {name!r}: {state!r},' + (f'  # {comment}' if comment else ''))
        out += ['}', '']
    if scan is not None:
        out += ['# The scan the Scan widget held (getScanParameters): the scanned axes',
                '# with lengths, steps and centres in µm, and the trigger pulses in s.',
                f'SCAN = {_fmt(scan, "SCAN = ")}', '']
    if stages or rotators:
        out += ['# Where the stages (µm) and rotators (degrees) were. Moving them there',
                '# is a separate decision: set MOVE_TO_POSITIONS = True to do it.',
                'MOVE_TO_POSITIONS = False']
        if stages:
            out.append(f'STAGE_POSITIONS = {_fmt(stages, "STAGE_POSITIONS = ")}')
        if rotators:
            out.append(f'ROTATOR_ANGLES = {_fmt(rotators, "ROTATOR_ANGLES = ")}')
        out.append('')
    if skipped:
        out += ['# Left out, because no Python literal spells them:']
        out += [f'#   {item}' for item in skipped]
        out.append('')

    out += ['# %% Apply the settings', '']
    out += ['for detector, detectorSettings in DETECTOR_SETTINGS.items():',
            '    for name, value in detectorSettings.items():',
            '        api.imcontrol.setDetectorParameter(detector, name, value)']
    if lasers:
        out += ['for laser, state in LASERS.items():',
                "    if 'value' in state:          # the value first: some lasers refuse",
                "        api.imcontrol.setLaserValue(laser, state['value'])  # to switch on at 0",
                "    api.imcontrol.setLaserActive(laser, state['on'])"]
    if scan is not None:
        out.append('api.imcontrol.setScanParameters(SCAN)')
    if stages or rotators:
        out.append('if MOVE_TO_POSITIONS:')
        if stages:
            out += ['    for stage, axes in STAGE_POSITIONS.items():',
                    '        for axis, position in axes.items():',
                    '            api.imcontrol.setPositioner(stage, axis, position)']
        if rotators:
            out += ['    for rotator, angle in ROTATOR_ANGLES.items():',
                    '        api.imcontrol.setRotatorPosition(rotator, angle)']
    out.append('')

    out += ['# %% Acquire', '']
    target = 'DETECTORS[0]' if len(detectors) == 1 else 'DETECTORS'
    if useScan:
        out += ['# The scan once, into memory: one frame per position it triggers.',
                f'frames = api.imcontrol.recordScanToMemory({target}, timeout=600)']
    else:
        out += [f'# {frames} frames into memory, as the Recording widget was set.',
                f'frames = api.imcontrol.recordFramesToMemory({target}, {frames}, timeout=600)']
    if len(detectors) > 1:
        out += ['# Several detectors: frames is a dict, {detector name: its frames}.',
                "show(frames, 'acquisition')"]
    else:
        out += ['print(f"{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px")',
                "show(frames, 'acquisition')"]
    text = '\n'.join(out).rstrip() + '\n'
    compile(text, '<acquisition script>', 'exec')   # never hand out a broken script
    return text


def _header(now, setupName, useScan, frames) -> List[str]:
    what = 'runs the scan once' if useScan else f'records {frames} frames'
    lines = ['"""Acquisition copied from ImSwitch on '
             f'{now:%Y-%m-%d at %H:%M}.', '']
    if setupName:
        lines += [f'Setup: {setupName}.']
    lines += ['These are the devices and settings ImSwitch had when the script was',
              f'made. Run all applies them and {what}, into memory;',
              'Run cell (Ctrl+Enter) does one step at a time. Nothing here is linked',
              'back to the widgets: edit freely.',
              '"""', '']
    return lines


def _detectorSettings(imcontrol, detector, skipped) -> Dict[str, Tuple[Any, str]]:
    found = {}
    for name, parameter in imcontrol.getDetectorParameters(detector).items():
        if (not parameter.get('editable') or parameter.get('value') is None
                or parameter.get('configOwned')):
            continue        # a calibration of the setup file is not a setting
        value = _literal(parameter['value'], f'{detector}: {name}', skipped)
        if value is _SKIP:
            continue
        found[name] = (value, parameter.get('units') or '')
    return found


def _lasers(imcontrol, skipped) -> Dict[str, Tuple[Dict[str, Any], str]]:
    lasers = {}
    for name in imcontrol.getLaserNames():
        info = imcontrol.getLaserInfo(name)
        state: Dict[str, Any] = {'on': bool(imcontrol.isLaserActive(name))}
        comment = f'{info["wavelength"]} nm' if info.get('wavelength') else ''
        if info.get('binary'):
            comment = ', '.join(filter(None, [comment, 'on/off only']))
        else:
            value = _literal(imcontrol.getLaserValue(name), f'{name}: value', skipped)
            if value is not _SKIP:
                state['value'] = value
            comment = ', '.join(filter(None, [info.get('units') or '', comment]))
        lasers[name] = (state, comment)
    return lasers


_SKIP = object()


def _literal(value, what, skipped):
    """``value`` as plain Python (NumPy scalars and arrays converted) when a
    literal spells it and reads back the same; otherwise _SKIP, noted."""
    plain = _plain(value)
    try:
        if ast.literal_eval(repr(plain)) == plain:
            return plain
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    skipped.append(f'{what} ({type(value).__name__})')
    return _SKIP


def _plain(value):
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_plain(v) for v in value)
    tolist = getattr(value, 'tolist', None)          # NumPy scalars and arrays
    if callable(tolist) and type(value).__module__ == 'numpy':
        return tolist()
    return value


def _fmt(value, prefix) -> str:
    """``value`` as a literal, wrapped to fit after ``prefix``."""
    text = pprint.pformat(value, width=_WIDTH - len(prefix), sort_dicts=False)
    return text.replace('\n', '\n' + ' ' * len(prefix))


def _has(obj, name) -> bool:
    try:
        getattr(obj, name)
    except AttributeError:
        return False
    return True


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

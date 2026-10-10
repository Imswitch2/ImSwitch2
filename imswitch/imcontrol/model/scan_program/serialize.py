"""Scan programs as JSON (docs/design/plans/scan-engines-and-expert-scan.md
§2.6, M3).

The Expert panel saves the program itself: ``{'format': 'scan-program/1',
...}`` with the model's own field names. What was requested is written as it
was requested; the realized schedule is never saved into it.

Loading is strict. An unknown key, a malformed value or a missing timing
policy refuses the file with every problem at once: a new-format program does
not silently gain compatibility exemptions (§2.6).

Qt-free.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Mapping

from .policy import PolicyError, resolve_policy
from .program import (
    DetectorBinding, DeviceDrive, Loop, Motion, ProgramError, ScanProgram, Separation,
    TimelineEvent, Wait, validate,
)

FORMAT = 'scan-program/1'

_TUPLES = {'parity_loops', 'table_um', 'still', 'after', 'before'}
_SETS = {'phases'}


def _plain(value):
    """JSON-safe: frozensets as sorted lists, tuples as lists."""
    if isinstance(value, (frozenset, set)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


def program_to_dict(program: ScanProgram) -> Dict[str, Any]:
    """The program as a JSON-safe dict, its request as requested."""
    data = _plain(program)
    return {'format': FORMAT, **data}


def _build(cls, data, where, problems, nested=None):
    if not isinstance(data, Mapping):
        problems.append(f'{where}: expected an object, not {type(data).__name__}')
        return None
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        problems.append(f'{where}: unknown keys {", ".join(unknown)}')
    kwargs = {}
    for key, value in data.items():
        if key not in names:
            continue
        if nested and key in nested:
            value = nested[key](value, f'{where}.{key}', problems)
        elif key in _SETS and value is not None:
            value = frozenset(value)
        elif key in _TUPLES and value is not None:
            value = tuple(value)
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as error:
        problems.append(f'{where}: {error}')
        return None


def _list(cls, nested=None):
    def build(values, where, problems):
        if not isinstance(values, list):
            problems.append(f'{where}: expected a list')
            return ()
        built = (_build(cls, value, f'{where}[{i}]', problems, nested)
                 for i, value in enumerate(values))
        return tuple(item for item in built if item is not None)
    return build


def _motion(value, where, problems):
    return _build(Motion, value, where, problems) or Motion()


def program_from_dict(data: Mapping[str, Any]) -> ScanProgram:
    """The program a ``program_to_dict`` dict describes, validated; or
    :class:`ProgramError` with every problem."""
    problems = []
    if not isinstance(data, Mapping):
        raise ProgramError(f'A scan program is an object, not {type(data).__name__}.')
    data = dict(data)
    fmt = data.pop('format', None)
    if fmt != FORMAT:
        raise ProgramError(f'This is not a scan program in the {FORMAT} format '
                           f'(format: {fmt!r}).')
    if not data.get('timing_policy'):
        raise ProgramError('The scan program names no timing policy; it is refused rather '
                           'than run under an assumed one.')
    try:
        resolve_policy(data['timing_policy'])
    except PolicyError as error:
        raise ProgramError(str(error)) from error
    program = _build(ScanProgram, data, 'program', problems, nested={
        'loops': _list(Loop),
        'drives': _list(DeviceDrive, nested={'motion': _motion}),
        'timeline': _list(TimelineEvent),
        'waits': _list(Wait),
        'separations': _list(Separation),
        'detectors': _list(DetectorBinding),
    })
    if problems or program is None:
        raise ProgramError('The scan program cannot be read:\n- ' + '\n- '.join(problems))
    return validate(program)


__all__ = ['FORMAT', 'program_from_dict', 'program_to_dict']


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

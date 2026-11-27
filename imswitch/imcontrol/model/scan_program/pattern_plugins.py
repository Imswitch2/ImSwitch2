"""Drop-in trajectory patterns (docs/design/plans/
scan-engines-and-expert-scan.md §4, §7.8).

A drop-in is a ``.py`` file in the user's ``scan_patterns`` folder
(``user_patterns_directory``) that defines ``PATTERNS``: a list of
``Pattern`` -- an id, a title, the devices it moves, its parameters as
``(name, label, kind)`` (its ``param_spec``: what the Expert panel's
**Make…** form asks for) and ``path(params, t, duration)``. Each becomes a
**Make… → Trajectory:** preset beside the built-in ones, and a saved program
that uses it opens wherever the file is loaded.

Loading is tolerant, as ImProcess's drop-ins: a file that fails to import,
or a pattern that would replace a built-in, reuse another's id, take a
parameter kind the form has no field for, or move other than two devices,
is reported with the reason and skipped -- one bad file never stops the
panel. Files are executed as arbitrary Python: only keep code you trust.
A pattern records where it came from (``Pattern.source``: the file and its
SHA-256), and a gridded image records that with the pattern's id.

Qt-free.
"""

from __future__ import annotations

import dataclasses
import glob
import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from imswitch.imcommon.model import dirtools
from . import presets
from .trajectory import PATTERNS, Pattern

#: The user files folder drop-in patterns live in.
SUBDIR = 'scan_patterns'

#: Written into the folder on first use; the underscore keeps it from loading.
TEMPLATE_NAME = '_example_pattern.py'

#: The parameter kinds the Make… form has a field for (``choice:a,b`` too).
PARAMETER_KINDS = ('micrometres', 'number', 'pair-micrometres', 'pair-hertz', 'points', 'code')
CHOICE_PREFIX = 'choice:'

#: The devices a trajectory moves: the form asks for an X and a Y device.
DIMENSIONS = 2

#: What the built-in patterns were before any drop-in, so a reload starts
#: from them.
_BUILTIN_PATTERNS = dict(PATTERNS)
_BUILTIN_PRESETS = dict(presets.PRESETS)

_TEMPLATE = '''\
"""An example drop-in trajectory pattern for the Expert scan panel.

Copy this file without the leading underscore (e.g. ``rosette.py``) and
edit it. Every .py file in this folder that does not start with an
underscore is loaded when the Expert panel starts; each Pattern in its
PATTERNS list becomes a "Make... -> Trajectory:" preset.

Files are executed as arbitrary Python: only keep code you trust.
"""

import numpy as np

from imswitch.imcontrol.model.scan_program.trajectory import Pattern


def rosette(params, t, duration):
    """x and y in micrometres for the times t (s, from the path's start)."""
    cx, cy = params['center_um']
    radius, petals = float(params['radius_um']), float(params['petals'])
    angle = 2 * np.pi * t / duration
    r = radius * np.cos(petals * angle)
    return np.vstack([cx + r * np.cos(angle), cy + r * np.sin(angle)])


PATTERNS = [
    # id (dotted, user-namespaced), title, devices moved (2), parameters as
    # (name, label, kind), and the path. Kinds: micrometres, number,
    # pair-micrometres, pair-hertz, points, code, choice:a,b
    Pattern('user.rosette', 'Rosette (example)', 2, (
        ('center_um', 'Centre', 'pair-micrometres'),
        ('radius_um', 'Radius', 'micrometres'),
        ('petals', 'Petals', 'number')), rosette),
]
'''


@dataclass(frozen=True)
class PatternLoadError:
    path: str
    message: str


@dataclass
class LoadedPatterns:
    ids: List[str] = field(default_factory=list)
    errors: List[PatternLoadError] = field(default_factory=list)


def user_patterns_directory(*, create: bool = True) -> str:
    """The drop-in folder, made with the example on first use (a home that
    cannot be written finds nothing, and that is all)."""
    directory = os.path.join(dirtools.UserFileDirs.Root, SUBDIR)
    if create:
        try:
            os.makedirs(directory, exist_ok=True)
            template = os.path.join(directory, TEMPLATE_NAME)
            if not os.path.exists(template):
                with open(template, 'w', encoding='utf-8') as handle:
                    handle.write(_TEMPLATE)
        except OSError:
            pass
    return directory


def _files(directory: str) -> List[str]:
    return [path for path in sorted(glob.glob(os.path.join(directory, '*.py')))
            if not os.path.basename(path).startswith('_')]


def _module(path: str):
    name = 'imswitch_scan_pattern_' + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot be imported from {path!r}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _problem(pattern, taken) -> Optional[str]:
    if not isinstance(pattern, Pattern):
        return f'{pattern!r} is not a Pattern'
    if pattern.id in _BUILTIN_PATTERNS:
        return f'{pattern.id!r} is a built-in pattern\'s id'
    if pattern.id in taken:
        return f'{pattern.id!r} is already defined by {taken[pattern.id]}'
    if pattern.dimensions != DIMENSIONS:
        return (f'{pattern.id!r} moves {pattern.dimensions} devices; a trajectory moves '
                f'{DIMENSIONS} (an X and a Y device)')
    if not callable(pattern.path):
        return f'{pattern.id!r} has no path function'
    for parameter in pattern.parameters:
        if len(parameter) != 3:
            return f'{pattern.id!r}: a parameter is (name, label, kind), not {parameter!r}'
        kind = str(parameter[2])
        if kind not in PARAMETER_KINDS and not (kind.startswith(CHOICE_PREFIX)
                                                and kind[len(CHOICE_PREFIX):]):
            return (f'{pattern.id!r}: parameter {parameter[0]!r} is of kind {kind!r}; the form '
                    f'takes {", ".join(PARAMETER_KINDS)} or {CHOICE_PREFIX}a,b')
    return None


def discover_patterns(directory: str):
    """``(patterns, errors)``: every valid pattern the folder's files define,
    each with its ``source``, and what was skipped with the reason."""
    found: Dict[str, Pattern] = {}
    where: Dict[str, str] = {}
    errors: List[PatternLoadError] = []
    for path in _files(directory):
        try:
            with open(path, 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            declared = getattr(_module(path), 'PATTERNS', None)
        except Exception as error:
            errors.append(PatternLoadError(path, f'it does not load: {error}'))
            continue
        if not isinstance(declared, (list, tuple)):
            errors.append(PatternLoadError(path, 'it defines no PATTERNS list'))
            continue
        for pattern in declared:
            problem = _problem(pattern, where)
            if problem is not None:
                errors.append(PatternLoadError(path, problem))
                continue
            found[pattern.id] = dataclasses.replace(
                pattern, source=f'drop-in {os.path.basename(path)} sha256:{digest}')
            where[pattern.id] = os.path.basename(path)
    return found, errors


def load_user_patterns(directory: Optional[str] = None) -> LoadedPatterns:
    """Scan the folder and make what it defines the drop-in patterns and
    their presets, replacing any earlier scan's."""
    directory = directory if directory is not None else user_patterns_directory()
    found, errors = discover_patterns(directory)
    PATTERNS.clear()
    PATTERNS.update(_BUILTIN_PATTERNS)
    PATTERNS.update(found)
    presets.PRESETS.clear()
    presets.PRESETS.update(_BUILTIN_PRESETS)
    for pattern_id in found:
        presets.PRESETS[f'trajectory-{pattern_id}'] = presets.trajectory_preset(pattern_id)
    return LoadedPatterns(sorted(found), errors)


__all__ = ['CHOICE_PREFIX', 'DIMENSIONS', 'LoadedPatterns', 'PARAMETER_KINDS',
           'PatternLoadError', 'discover_patterns', 'load_user_patterns',
           'user_patterns_directory']


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

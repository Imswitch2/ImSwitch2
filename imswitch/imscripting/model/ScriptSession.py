"""The interactive session of ImScripting.

*Run all* executes a script in a fresh namespace every time, so a full run is
reproducible. *Run selection* and *Run cell* execute in one namespace that
lives on between runs -- the session -- and the console works in that same
namespace, so a variable one selection creates is there for the next one and
for the console: acquire, inspect, change something, acquire again.

The session's namespace is one dict for the life of the module. ``reset()``
empties it and puts back what ImSwitch installs (``api``, the helpers, ...);
it never replaces the dict, because the console holds a reference to it.
The session is mutated on the script thread while a run executes and read on
the GUI thread only while no run executes; the editor enforces that by
disabling the console and the Reset action during runs.
"""

import linecache
import re
import textwrap
import types
from typing import Any, Dict, List, Optional, Tuple

from .actions import getActionsScope

#: A cell starts at a line that begins with ``# %%`` (Spyder, VS Code,
#: PyCharm and Jupytext all use this marker).
CELL_MARKER = re.compile(r'^\s*#\s*%%')


class ScriptSession:
    """The namespace shared by Run selection, Run cell and the console."""

    def __init__(self, baseScope: Dict[str, Any]):
        self._baseScope = baseScope
        self.namespace: Dict[str, Any] = {}
        self._installed = set()
        self.reset()

    def reset(self) -> None:
        """Forgets every variable and puts back what ImSwitch installs. Only
        call it while no run executes in the session."""
        self.namespace.clear()
        self.namespace.update(self._baseScope)
        self._installed = set(self.namespace)

    def prepareRun(self, scriptPath: Optional[str]) -> Dict[str, Any]:
        """Script thread, before a run: (re)installs the helpers bound to
        this run's script file (``importScript`` resolves paths against it)
        and returns the namespace to execute in."""
        actions = getActionsScope(self._baseScope, scriptPath)
        self.namespace.update(actions)
        self._installed.update(actions)
        return self.namespace

    def isInstalled(self, name: str) -> bool:
        """Whether ``name`` is one of the names ImSwitch puts into the
        session rather than one a script or the console defined."""
        return name in self._installed

    def userVariables(self) -> Dict[str, Any]:
        """The variables scripts and the console defined, in definition
        order: no names ImSwitch installed, no private names, and no modules,
        functions or classes -- the data a user inspects. Read it only while
        no run executes."""
        return {
            name: value for name, value in list(self.namespace.items())
            if not name.startswith('_')
            and name not in self._installed
            and not isinstance(value, (types.ModuleType, types.FunctionType,
                                       types.BuiltinFunctionType, type))
        }


def describeValue(value: Any) -> Dict[str, str]:
    """What the variable inspector shows for a value: its ``type``, its
    ``shape``, its ``dtype``, its ``size`` in memory and a short ``value``.
    Fields that do not apply are empty strings."""
    import numpy as np

    info = {'type': type(value).__name__, 'shape': '', 'dtype': '', 'size': '',
            'value': ''}
    if isinstance(value, np.ndarray):
        info.update(shape=' x '.join(str(n) for n in value.shape) or 'scalar',
                    dtype=str(value.dtype), size=formatBytes(value.nbytes))
        if value.size:
            info['value'] = f'{_number(value.min())} to {_number(value.max())}'
        return info
    if isinstance(value, dict):
        arrays = [v for v in value.values() if isinstance(v, np.ndarray)]
        info['shape'] = f'{len(value)} entries'
        if arrays:
            info['size'] = formatBytes(sum(a.nbytes for a in arrays))
        info['value'] = _short(', '.join(str(k) for k in value))
        return info
    if isinstance(value, (list, tuple, set)):
        info['shape'] = f'{len(value)} items'
    info['value'] = _short(repr(value))
    return info


def isShowable(value: Any) -> bool:
    """Whether ``show()`` can display the value: an array of two or more
    dimensions, or a dict holding such arrays."""
    import numpy as np

    if isinstance(value, np.ndarray):
        return value.ndim >= 2
    if isinstance(value, dict):
        return any(isinstance(v, np.ndarray) and v.ndim >= 2 for v in value.values())
    return False


def formatBytes(nbytes: int) -> str:
    for unit, factor in (('GB', 1 << 30), ('MB', 1 << 20), ('kB', 1 << 10)):
        if nbytes >= factor:
            return f'{nbytes / factor:.1f} {unit}'
    return f'{nbytes} B'


def cellAt(text: str, line: int) -> Tuple[int, int, str]:
    """The cell around ``line`` (0-based) of ``text``: ``(first, end, code)``
    with ``first`` the cell's first line, ``end`` one past its last, and
    ``code`` its lines. A cell runs from a ``# %%`` line (or the top of the
    file) to the line before the next one (or the end of the file); a cursor
    on a marker line is in the cell the marker opens."""
    lines = text.split('\n')
    line = max(0, min(line, len(lines) - 1))
    first = line
    while first > 0 and not CELL_MARKER.match(lines[first]):
        first -= 1
    end = line + 1
    while end < len(lines) and not CELL_MARKER.match(lines[end]):
        end += 1
    return first, end, '\n'.join(lines[first:end])


def cellStarts(text: str) -> List[int]:
    """The 0-based lines where cells start, the first line always included."""
    lines = text.split('\n')
    return [0] + [i for i, l in enumerate(lines) if i and CELL_MARKER.match(l)]


def compileForEditor(code: str, firstLine: int, name: str) -> types.CodeType:
    """Compiles a part of an editor's text so that a traceback names the
    editor's own line numbers and shows the code as it is in the editor, not
    as last saved: ``code`` starts at the 0-based ``firstLine``, a selection
    that starts indented is dedented, and the text is registered with
    linecache under ``name``."""
    if code.lstrip('\n')[:1] in (' ', '\t'):
        code = textwrap.dedent(code)
    padded = '\n' * firstLine + code
    if not padded.endswith('\n'):
        padded += '\n'
    linecache.cache[name] = (len(padded), None, padded.splitlines(True), name)
    return compile(padded, name, 'exec')


def editorFileName(scriptPath: Optional[str]) -> str:
    """The file name tracebacks give code run from the editor."""
    if not scriptPath:
        return '<untitled script>'
    return f'<{scriptPath}>'


def _number(value) -> str:
    try:
        return f'{float(value):.4g}'
    except (TypeError, ValueError):
        return str(value)


def _short(text: str, limit: int = 80) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


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

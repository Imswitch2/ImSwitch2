"""Inputs that a script declares with ``scriptInput(name, default, description)``.

The declarations are found by parsing the script's source, never by running
it: scripts move hardware, so listing what a script asks for must not
execute a line of it. Only calls with a literal name are understood; the
default must be a literal too for its type (and so the conversion of typed
text) to be known.

Shared by imscripting (which hands the values to the running script) and
imcontrol (whose ScriptRunner widget builds a form from the declarations).
"""

import ast
from dataclasses import dataclass, field
from typing import Any, List, Optional

#: Name of the scripting action that declares an input.
SCRIPT_INPUT_FUNCTION = 'scriptInput'

_ARGUMENT_NAMES = ('name', 'default', 'description')
_TRUE_TEXTS = ('true', '1', 'yes', 'on')
_FALSE_TEXTS = ('false', '0', 'no', 'off')


@dataclass(frozen=True)
class ScriptInputSpec:
    """ One ``scriptInput`` declaration. ``type`` is that of the default
    (``bool``, ``int``, ``float`` or ``str``); an input whose default is not
    a literal, is ``None`` or of another type is entered as text. """
    name: str
    default: Any = None
    description: str = ''
    type: type = str
    line: int = 0
    defaultKnown: bool = True

    def defaultText(self) -> str:
        """ The default as the text a user would type for it. """
        if not self.defaultKnown or self.default is None:
            return ''
        return str(self.default)


@dataclass
class ScriptInputScan:
    """ Result of :func:`findScriptInputs`. ``error`` is set when the source
    could not be parsed at all; ``warnings`` name declarations that were
    skipped or only partly understood. """
    inputs: List[ScriptInputSpec] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


def findScriptInputs(source: str) -> ScriptInputScan:
    """ Lists the ``scriptInput(...)`` calls in ``source`` in the order they
    appear. A name declared twice is listed once, at its first use. """
    scan = ScriptInputScan()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        scan.error = f'Syntax error on line {e.lineno}: {e.msg}'
        return scan

    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == SCRIPT_INPUT_FUNCTION]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))

    seen = {}
    for call in calls:
        spec = _specFromCall(call, scan.warnings)
        if spec is None:
            continue
        first = seen.get(spec.name)
        if first is None:
            seen[spec.name] = spec
            scan.inputs.append(spec)
        elif (first.default, first.defaultKnown) != (spec.default, spec.defaultKnown):
            scan.warnings.append(
                f'Line {spec.line}: "{spec.name}" is declared again with another '
                f'default; the one on line {first.line} is used.'
            )
    return scan


def _specFromCall(call, warnings):
    arguments = dict(zip(_ARGUMENT_NAMES, call.args))
    for keyword in call.keywords:
        if keyword.arg in _ARGUMENT_NAMES:
            arguments[keyword.arg] = keyword.value

    nameNode = arguments.get('name')
    name = _literal(nameNode)
    if not isinstance(name, str) or not name:
        warnings.append(
            f'Line {call.lineno}: {SCRIPT_INPUT_FUNCTION}() needs a quoted name as its '
            'first argument; this one is skipped.'
        )
        return None

    description = _literal(arguments.get('description'))
    if not isinstance(description, str):
        description = ''

    defaultNode = arguments.get('default')
    if defaultNode is None:
        return ScriptInputSpec(name=name, description=description, line=call.lineno)
    try:
        default = ast.literal_eval(defaultNode)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        warnings.append(
            f'Line {call.lineno}: the default of "{name}" is not a plain value, so '
            'it is entered as text; leave the field empty to use the default.'
        )
        return ScriptInputSpec(name=name, description=description, line=call.lineno,
                               defaultKnown=False)
    return ScriptInputSpec(name=name, default=default, description=description,
                           type=_inputType(default), line=call.lineno)


def _literal(node):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _inputType(default):
    # bool before int: bool is a subclass of int.
    for candidate in (bool, int, float):
        if isinstance(default, candidate):
            return candidate
    return str


def convertInputText(text: str, inputType: type) -> Any:
    """ Converts typed text to a value of ``inputType``. Raises ValueError
    with a message meant for the user when the text does not fit. """
    stripped = text.strip()
    if inputType is bool:
        lowered = stripped.lower()
        if lowered in _TRUE_TEXTS:
            return True
        if lowered in _FALSE_TEXTS:
            return False
        raise ValueError('Expected True or False')
    if inputType is int:
        try:
            return int(stripped)
        except ValueError:
            raise ValueError('Expected a whole number') from None
    if inputType is float:
        try:
            return float(stripped)
        except ValueError:
            raise ValueError('Expected a number') from None
    return text


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

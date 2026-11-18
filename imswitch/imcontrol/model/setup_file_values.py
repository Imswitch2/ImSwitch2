"""Tolerate a blank or unreadable value in a typed setup-file field.

``loadSetupInfo()`` decodes the setup file with dataclasses_json, which
coerces a scalar field with ``int(v)`` / ``float(v)`` / ``bool(v)``. So a
blank ``"lineClockTriggerV": ""`` raised ``could not convert string to
float`` and Hardware Control refused to start, and ``"simulation": "false"``
decoded as ``True`` -- the opposite of what the file says.

A small slip in a setup file must never stop Hardware Control. Before
decoding, :func:`sanitizeSetupData` walks the dataclass type hints of the
setup type over the parsed JSON and, for a value the field's type cannot
read, drops the key so ``infer_missing`` gives the declared default. Each
fix is reported as one message naming the JSON path, the bad value and what
is used instead; the caller logs them. The file on disk is never rewritten.

What is touched, by declared type (``Optional[...]`` of each included):

* ``int`` / ``float`` (and a ``Union`` of only ``int`` and ``float``): a
  blank or unparseable string, a non-finite number, or a list/object is
  dropped. A numeric string such as ``"0.3"`` still reads as the number, as
  it did before.
* ``bool``: the strings ``true/false/yes/no/on/off/1/0`` (any case) and the
  numbers ``1``/``0`` become real booleans; anything else is dropped.
* A nested setup dataclass: walked; a value that is not a JSON object (a
  blank ``"focusLock": ""``) is dropped.
* ``Dict[str, X]``: each value is checked as an ``X``; an unreadable one
  drops that entry.
* ``List[X]`` of setup dataclasses: each object element is walked in place.

Everything else is left exactly as written: free-form ``Dict[str, Any]``
such as ``managerProperties``, strings, enums, unions with ``str`` (channel
names), and lists of scalars, whose elements are never dropped because that
would shift the meaning of every later element.
"""

import copy
import dataclasses
import functools
import math
import types
import typing
from typing import Any, List, Tuple


_TRUE_WORDS = frozenset({'true', 'yes', 'on', '1'})
_FALSE_WORDS = frozenset({'false', 'no', 'off', '0'})

_NONE_TYPE = type(None)


class _Drop:
    """ Marker: leave this key out so the declared default is used. """


_DROP = _Drop()


def sanitizeSetupData(data: Any, setupInfoType: type) -> Tuple[Any, List[str]]:
    """Return a cleaned copy of parsed setup-file JSON and the fixes made.

    ``data`` is what ``json.loads`` produced for the whole file;
    ``setupInfoType`` is the dataclass it will be decoded into (``SetupInfo``
    or ``ViewSetupInfo``). The input is not modified. A top level that is not
    a JSON object is returned unchanged for the decoder to reject as before.
    """
    if not isinstance(data, dict) or not dataclasses.is_dataclass(setupInfoType):
        return data, []
    cleaned = copy.deepcopy(data)
    fixes: List[str] = []
    _walkDataclass(cleaned, setupInfoType, '', fixes)
    return cleaned, fixes


@functools.lru_cache(maxsize=None)
def _typeHints(cls) -> dict:
    try:
        return typing.get_type_hints(cls)
    except Exception:
        # An unresolvable forward reference must not cost the whole walk:
        # fall back to the annotations that are already real types.
        return {
            f.name: f.type for f in dataclasses.fields(cls)
            if not isinstance(f.type, str)
        }


def _joinPath(path: str, key) -> str:
    return f'{path}.{key}' if path else str(key)


def _fieldDefaultText(fieldInfo) -> str:
    if fieldInfo.default is not dataclasses.MISSING:
        return f'using the default {fieldInfo.default!r}'
    if fieldInfo.default_factory is not dataclasses.MISSING:
        try:
            return f'using the default {fieldInfo.default_factory()!r}'
        except Exception:
            return 'using the default'
    return 'left unset (the field has no default)'


def _walkDataclass(obj: dict, cls, path: str, fixes: List[str]) -> None:
    hints = _typeHints(cls)
    for fieldInfo in dataclasses.fields(cls):
        name = fieldInfo.name
        if name not in obj or name not in hints:
            continue
        result = _clean(
            obj[name], hints[name], _joinPath(path, name), fixes,
            lambda fieldInfo=fieldInfo: _fieldDefaultText(fieldInfo),
        )
        if result is _DROP:
            del obj[name]
        else:
            obj[name] = result


def _unionArgs(tp):
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        return typing.get_args(tp)
    return None


def _clean(value, tp, path: str, fixes: List[str], instead):
    """Return the value to decode, or ``_DROP``.

    ``instead`` is a callable describing what replaces a dropped value (the
    field default, or the entry being ignored); it is only evaluated when a
    fix is reported.
    """
    if value is None:
        # null is the decoder's business, exactly as before.
        return value

    unionArgs = _unionArgs(tp)
    if unionArgs is not None:
        members = [arg for arg in unionArgs if arg is not _NONE_TYPE]
        if len(members) == 1:
            tp = members[0]
        elif members and all(member in (int, float) for member in members):
            return _cleanNumber(value, 'number', path, fixes, instead)
        else:
            # Union with str, lists, ... : free-form, leave it alone.
            return value

    if tp is bool:
        return _cleanBool(value, path, fixes, instead)
    if tp is int:
        return _cleanNumber(value, 'whole number', path, fixes, instead)
    if tp is float:
        return _cleanNumber(value, 'number', path, fixes, instead)

    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        if isinstance(value, dict):
            _walkDataclass(value, tp, path, fixes)
            return value
        fixes.append(
            f'{path} = {value!r} is not a section (a JSON object); '
            f'{instead()}.'
        )
        return _DROP

    origin = typing.get_origin(tp)
    if origin is dict:
        args = typing.get_args(tp)
        if len(args) == 2 and isinstance(value, dict):
            valueType = args[1]
            for key in list(value):
                result = _clean(
                    value[key], valueType, _joinPath(path, key), fixes,
                    lambda: 'entry ignored',
                )
                if result is _DROP:
                    del value[key]
                else:
                    value[key] = result
        return value
    if origin is list:
        args = typing.get_args(tp)
        if len(args) == 1 and isinstance(value, list):
            elementType = args[0]
            elementArgs = _unionArgs(elementType)
            if elementArgs is not None:
                members = [arg for arg in elementArgs if arg is not _NONE_TYPE]
                elementType = members[0] if len(members) == 1 else None
            if isinstance(elementType, type) and dataclasses.is_dataclass(elementType):
                for index, element in enumerate(value):
                    if isinstance(element, dict):
                        _walkDataclass(element, elementType, f'{path}[{index}]', fixes)
        return value

    return value


def _cleanBool(value, path: str, fixes: List[str], instead):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS or word in _FALSE_WORDS:
            result = word in _TRUE_WORDS
            fixes.append(
                f'{path} = {value!r} is a string, not true/false; '
                f'read as {str(result).lower()}.'
            )
            return result
    elif isinstance(value, int) and value in (0, 1):
        return bool(value)
    fixes.append(f'{path} = {value!r} is not true or false; {instead()}.')
    return _DROP


def _cleanNumber(value, kind: str, path: str, fixes: List[str], instead):
    """``kind`` is ``'whole number'`` for ``int``, ``'number'`` otherwise."""
    if isinstance(value, bool):
        # true/false in a number field decodes as 1/0, as it always did.
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            fixes.append(f'{path} = {value!r} is not a finite {kind}; {instead()}.')
            return _DROP
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            fixes.append(f'{path} = {value!r} is blank; {instead()}.')
            return _DROP
        number = _parseNumber(text, wholeOnly=(kind == 'whole number'))
        if number is None:
            fixes.append(f'{path} = {value!r} is not a {kind}; {instead()}.')
            return _DROP
        if isinstance(number, float) and not math.isfinite(number):
            fixes.append(f'{path} = {value!r} is not a finite {kind}; {instead()}.')
            return _DROP
        return number
    fixes.append(f'{path} = {value!r} is not a {kind}; {instead()}.')
    return _DROP


def _parseNumber(text: str, *, wholeOnly: bool):
    """Parse a numeric string the way the decoder would: ``int()`` for an
    ``int`` field (so ``"5.0"`` stays unreadable there, as before), ``int()``
    then ``float()`` otherwise."""
    try:
        return int(text)
    except ValueError:
        if wholeOnly:
            return None
    try:
        return float(text)
    except ValueError:
        return None


# Copyright (C) 2020-2021 ImSwitch developers
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

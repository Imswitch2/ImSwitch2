"""Micro-Manager device properties <-> ImSwitch2 detector parameters.

Pure functions on a ``CMMCorePlus`` (or anything with its property API) and a
device label; no manager state. The caller holds the host lock around them
(``with host.lock:``), exactly as for any other MMCore call.

Mapping rules (plan ``micromanager-bridge.md`` section 4.4), applied in this order:

1. Pre-initialisation properties are skipped: they can only be set before the
   device is initialised, which the setup file's ``preInitProperties`` does.
2. A property with **allowed values** becomes a ``DetectorListParameter`` whose
   options are those values (as the strings MMCore reports) and whose value is
   the current one. This also covers integer-typed properties such as
   ``Binning`` (``'1'``, ``'2'``, ...): a list parameter always carries strings.
3. An ``Integer`` / ``Float`` property without allowed values becomes a
   ``DetectorNumberParameter`` (value = current, typed ``int`` / ``float``).
   Property limits are not carried: ``DetectorNumberParameter`` has no range,
   and MMCore refuses an out-of-range write itself with a clear message.
4. A ``String`` property without allowed values becomes a **read-only**
   ``DetectorListParameter`` with the current value as its only option: free
   text has no parameter type in ImSwitch2.
5. A read-only property is never editable.

Micro-Manager properties carry **no units**, so ``valueUnits`` is the empty
string for every mapped number; a unit is only known for the first-class
parameters the camera manager builds itself (exposure in ms).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Union

from imswitch.pluginapi import DetectorListParameter, DetectorNumberParameter, DetectorParameter

#: Parameter group of every mapped Micro-Manager property in the settings tree.
PROPERTY_GROUP = 'Camera properties'

#: ``valueUnits`` of a mapped number: Micro-Manager properties have no units
#: (see the module docstring), and no unit is better than a guessed one.
NO_UNITS = ''

PropertyValue = Union[int, float, str]


def _property_type(core, label: str, name: str):
    from pymmcore_plus import PropertyType
    return PropertyType(core.getPropertyType(label, name))


def has_allowed_values(core, label: str, name: str) -> bool:
    """Whether the property is a choice (rule 2): MMCore lists allowed values for it."""
    return len(core.getAllowedPropertyValues(label, name)) > 0


def build_parameters(core, label: str, *, exclude: Iterable[str] = ()) -> Dict[str, DetectorParameter]:
    """One ``DetectorParameter`` per usable property of device ``label``, in MMCore's order.

    ``exclude`` names properties that are mapped elsewhere as first-class
    parameters (``Exposure``, ``Binning``). The parameter name is the
    Micro-Manager property name, unchanged.
    """
    from pymmcore_plus import PropertyType

    skipped = set(exclude)
    parameters: Dict[str, DetectorParameter] = {}
    for name in core.getDevicePropertyNames(label):
        if name in skipped or core.isPropertyPreInit(label, name):
            continue
        read_only = bool(core.isPropertyReadOnly(label, name))
        current = read_back(core, label, name)
        if has_allowed_values(core, label, name):
            options = [str(option) for option in core.getAllowedPropertyValues(label, name)]
            if current not in options:               # a value MMCore holds but does not list: keep it selectable
                options.append(current)
            parameters[name] = DetectorListParameter(
                group=PROPERTY_GROUP, value=current, options=options, editable=not read_only)
        elif _property_type(core, label, name) in (PropertyType.Integer, PropertyType.Float):
            parameters[name] = DetectorNumberParameter(
                group=PROPERTY_GROUP, value=current, valueUnits=NO_UNITS, editable=not read_only)
        else:
            parameters[name] = DetectorListParameter(
                group=PROPERTY_GROUP, value=str(current), options=[str(current)], editable=False)
    return parameters


def read_back(core, label: str, name: str) -> PropertyValue:
    """The property's current value, typed the way :func:`build_parameters` stores it.

    A choice property (allowed values) is always a ``str``; otherwise
    ``Integer`` -> ``int``, ``Float`` -> ``float``, everything else ``str``.
    """
    from pymmcore_plus import PropertyType

    raw = core.getProperty(label, name)
    if has_allowed_values(core, label, name):
        return str(raw)
    kind = _property_type(core, label, name)
    if kind == PropertyType.Integer:
        try:
            return int(raw)
        except ValueError:                           # an adapter that prints an integer as '3.0000'
            return int(float(raw))
    if kind == PropertyType.Float:
        return float(raw)
    return str(raw)


def coerce_for_mm(param: DetectorParameter, value) -> str:
    """The string MMCore's ``setProperty`` wants for ``value`` of parameter ``param``.

    * a list parameter takes the value as text and must be one of its options
      (unless the parameter is a read-only echo, which is refused anyway);
    * an integer-valued number parameter refuses a non-integral value instead
      of rounding it;
    * a float-valued number parameter refuses NaN and infinity.

    Raises ``ValueError`` (``TypeError`` for a non-number given to a number
    parameter); nothing here talks to MMCore.
    """
    if not getattr(param, 'editable', True):
        raise ValueError('the property is read-only')
    if isinstance(param, DetectorListParameter):
        text = str(value)
        if isinstance(value, float) and value.is_integer() and text not in param.options:
            text = str(int(value))                   # 2.0 for the choice '2'
        if text not in param.options:
            raise ValueError(f'{text!r} is not one of the allowed values {list(param.options)}')
        return text
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise TypeError(f'{value!r} is not a number') from None
    if isinstance(param.value, int) and not isinstance(param.value, bool):
        if float(value) != int(value):
            raise ValueError(f'{value!r} is not an integer; the property takes whole numbers')
        return str(int(value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{value!r} is not a finite number')
    return repr(number)


def refresh_parameters(core, label: str, parameters: Dict[str, DetectorParameter],
                       names: Iterable[str]) -> List[str]:
    """Re-read ``names`` from the device into ``parameters`` (in place).

    Returns the names whose value changed. A list parameter whose new value is
    not among its options gets it appended (a read-only echo such as a
    description changes by itself).
    """
    changed: List[str] = []
    for name in names:
        param = parameters.get(name)
        if param is None:
            continue
        value = read_back(core, label, name)
        if isinstance(param, DetectorListParameter) and value not in param.options:
            param.options.append(value)
        if value != param.value:
            param.value = value
            changed.append(name)
    return changed

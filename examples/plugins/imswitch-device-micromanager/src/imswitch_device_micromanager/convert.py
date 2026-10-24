"""Micro-Manager ``.cfg`` -> ImSwitch2 setup-file skeleton (plan ``micromanager-bridge.md`` 4.6).

``python -m imswitch_device_micromanager convert MMConfig.cfg -o setup.json`` reads the
configuration with the grammar MMCore's ``loadSystemConfiguration`` parses and writes

- ``setup.json``: the hub (``rs232devices``, pointing at the ``.cfg``) and one entry per device the
  bridge can map: Camera -> ``detectors``, Stage / XYStage -> ``positioners``, Shutter -> ``lasers``,
  two-position State device -> ``flipMirrors``;
- ``setup.conversion-report.txt`` (also printed): one line per device that was not written, with its
  label, adapter library, device name, type and the reason, and one line per value the converter could
  not derive (a TODO).

Pure Python: no Qt, no ``imswitch`` import, no device is loaded or initialised.

Types. A ``Device,`` line carries the label, the adapter library and the device name, not the device
*type*. :func:`classify` asks a bare MMCore (adapters on the search path, nothing loaded) what the
library says -- ``getAvailableDevices(library)`` next to ``getAvailableDeviceTypes(library)``. Without
the adapters on the converting machine, or for a library or device name MMCore cannot resolve, the
device is *unresolved*: it is listed in the report and not written. ``--type LABEL=TYPE`` supplies or
overrides a type by hand; ``--no-classify`` skips the lookup altogether.

How a value the converter cannot derive is written (nothing here invents a number, and the JSON never
carries a comment: ``validate-setup`` loads it with ``json.load``):

- ``cameraPixelSizeUm`` -- **omitted**. The camera manager then takes Micro-Manager's pixel-size config
  when the ``.cfg`` defines one (the report says so), otherwise the pixel size is unknown (the report
  lists it as a TODO).
- the axis name of a Stage that is not ``Core,Focus`` -- the **string** ``"TODO"``: ``axes`` is
  required, a name is the user's choice, and a string no scan or widget will mistake for an axis loads
  fine. Listed in the report.
- a light's ``wavelength`` -- **omitted**. ``infer_missing`` (the loader's mode) reads it as ``None``;
  ``validate-setup`` shows one warning per light, which is the TODO itself. The report lists it.
  ``valueRangeMin`` / ``valueRangeMax`` are ``null``: that is a binary light's own value, not a gap.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ._core import (
    SIGNALS_BACKEND,
    SIGNALS_BACKEND_ENV,
    MMAdaptersNotFoundError,
    MMCoreHost,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------- .cfg grammar
CMD_DEVICE = 'Device'
CMD_PARENT = 'Parent'
CMD_PROPERTY = 'Property'
CMD_LABEL = 'Label'
CMD_CONFIG_GROUP = 'ConfigGroup'
CMD_CONFIG_PIXEL_SIZE = 'ConfigPixelSize'
CMD_PIXEL_SIZE_UM = 'PixelSize_um'
CMD_PIXEL_SIZE_AFFINE = 'PixelSizeAffine'
CMD_DELAY = 'Delay'
CMD_FOCUS_DIRECTION = 'FocusDirection'

#: Field counts (command included) each command may have. MMCore splits on ',' only: there is no
#: quoting, so a value with a comma is a parse error in Micro-Manager too. ``Property`` may have an
#: empty value (3 fields); ``ConfigGroup`` may only declare a group (2) or a preset (3).
FIELD_COUNTS: Dict[str, Tuple[int, ...]] = {
    CMD_DEVICE: (4,),
    CMD_PARENT: (3,),
    CMD_PROPERTY: (3, 4),
    CMD_LABEL: (4,),
    CMD_CONFIG_GROUP: (2, 3, 5, 6),
    CMD_CONFIG_PIXEL_SIZE: (2, 4, 5),
    CMD_PIXEL_SIZE_UM: (3,),
    CMD_PIXEL_SIZE_AFFINE: (8,),
    CMD_DELAY: (3,),
    CMD_FOCUS_DIRECTION: (3,),
}

#: The device label MMCore uses for its own properties (``Property,Core,Initialize,1`` ...).
CORE_LABEL = 'Core'
PROP_INITIALIZE = 'Initialize'
#: ``Property,Core,<role>,<label>`` assigns a device to a core role.
CORE_ROLES = ('Camera', 'Shutter', 'Focus', 'XYStage', 'SLM', 'Galvo', 'ImageProcessor', 'AutoFocus')

#: The pre-init property DemoCamera's State devices (and several other adapters) carry; the one
#: authoritative source of a position count that needs no hardware. Adapters that name it otherwise
#: fall back to the ``Label`` lines.
POSITIONS_PROPERTY = 'Number of positions'


class CfgParseError(ValueError):
    """A ``.cfg`` line MMCore could not read; carries the 1-based line number."""

    def __init__(self, path: str, line_number: int, message: str, line: str = ''):
        self.path, self.line_number, self.line = path, line_number, line
        where = f'{path}:{line_number}'
        super().__init__(f'{where}: {message}' + (f' (line: {line!r})' if line else ''))


@dataclass
class CfgDevice:
    """One ``Device,`` line plus everything the file says about that label."""

    label: str
    library: str
    name: str
    line: int
    parent: Optional[str] = None
    #: properties set before the ``Initialize,1`` marker (MM "pre-init" properties), in file order
    pre_init: Dict[str, str] = field(default_factory=dict)
    #: properties set after it
    properties: Dict[str, str] = field(default_factory=dict)
    #: ``Label,<label>,<state>,<text>``: state index -> text
    state_labels: Dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionCount:
    count: int
    source: str


@dataclass
class PixelSizeConfig:
    """``ConfigPixelSize,<name>,...`` settings and the ``PixelSize_um,<name>,<value>`` they select."""

    name: str
    settings: List[Tuple[str, str, str]] = field(default_factory=list)      # (device, property, value)
    size_um: Optional[float] = None


@dataclass
class CfgModel:
    path: str
    devices: Dict[str, CfgDevice] = field(default_factory=dict)
    #: ``Property,Core,<name>,<value>`` lines other than ``Initialize``: the roles, ``AutoShutter`` ...
    core_properties: Dict[str, str] = field(default_factory=dict)
    pixel_sizes: Dict[str, PixelSizeConfig] = field(default_factory=dict)
    #: group -> preset -> [(device, property, value)]
    config_groups: Dict[str, Dict[str, List[Tuple[str, str, str]]]] = field(default_factory=dict)
    delays: Dict[str, str] = field(default_factory=dict)
    focus_directions: Dict[str, str] = field(default_factory=dict)
    #: Things that were read but not understood or not consistent; the report lists them.
    warnings: List[str] = field(default_factory=list)

    @property
    def roles(self) -> Dict[str, str]:
        """Core roles the file assigns (``{'Camera': 'Camera', 'Focus': 'Z', ...}``)."""
        return {role: self.core_properties[role] for role in CORE_ROLES if self.core_properties.get(role)}

    def position_count(self, label: str) -> Tuple[Optional[PositionCount], Optional[str]]:
        """``(count, problem)`` of a State device from the file alone (no hardware).

        The pre-init property ``Number of positions`` is authoritative when present. Otherwise the
        ``Label`` lines are evidence (the Configurator writes one per state; a hand-edited file may
        define fewer, in which case the device refuses at startup with its real count). Labels that
        point beyond the pre-init count contradict it: no count then, with the reason as ``problem``.
        """
        device = self.devices[label]
        declared = device.pre_init.get(POSITIONS_PROPERTY) or device.properties.get(POSITIONS_PROPERTY)
        from_labels = (max(device.state_labels) + 1) if device.state_labels else None
        if declared is not None:
            try:
                count = int(float(declared))
            except ValueError:
                return None, f"property {POSITIONS_PROPERTY!r} is {declared!r}, not a number"
            if from_labels is not None and from_labels > count:
                return None, (f"property {POSITIONS_PROPERTY!r} says {count} but the Label lines go up to "
                              f"state {from_labels - 1}")
            return PositionCount(count, f"property {POSITIONS_PROPERTY!r}"), None
        if from_labels is not None:
            return PositionCount(from_labels, f"{len(device.state_labels)} Label line(s)"), None
        return None, f"the file has neither a {POSITIONS_PROPERTY!r} property nor Label lines for it"


def parse_cfg(path) -> CfgModel:
    """Parse a Micro-Manager ``.cfg`` file; :class:`CfgParseError` names the first bad line."""
    text = Path(str(path)).expanduser().read_text(encoding='utf-8', errors='replace')
    return parse_cfg_text(text, str(path))


def parse_cfg_text(text: str, path: str = '<text>') -> CfgModel:
    """Parse ``.cfg`` text with MMCore's grammar (see the module docstring and :data:`FIELD_COUNTS`)."""
    model = CfgModel(path=path)
    initialised = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split(',')
        command = fields[0]
        if command not in FIELD_COUNTS:
            model.warnings.append(f'line {number}: unknown command {command!r} ignored')
            continue
        if len(fields) not in FIELD_COUNTS[command]:
            raise CfgParseError(
                path, number, f'{command} takes {_count_text(FIELD_COUNTS[command])} comma-separated fields '
                f'(command included), found {len(fields)}', line)
        try:
            initialised = _apply_line(model, number, fields, initialised)
        except _LineError as exc:
            raise CfgParseError(path, number, str(exc), line) from None
    return model


def _count_text(counts: Sequence[int]) -> str:
    return ' or '.join(str(c) for c in counts)


class _LineError(ValueError):
    """A problem with the content of one line; :func:`parse_cfg_text` adds file and line number."""


def _fail(message: str) -> _LineError:
    return _LineError(message)


def _apply_line(model: CfgModel, number: int, fields: List[str], initialised: bool) -> bool:
    command = fields[0]
    if command == CMD_DEVICE:
        _, label, library, name = fields
        if not label or not library or not name:
            raise _fail('Device needs a label, an adapter library and a device name')
        if label in model.devices:
            raise _fail(f'device label {label!r} is declared twice (first on line {model.devices[label].line})')
        model.devices[label] = CfgDevice(label, library, name, number)
    elif command == CMD_PARENT:
        device = _device(model, fields[1], command)
        device.parent = fields[2]
    elif command == CMD_PROPERTY:
        label, prop = fields[1], fields[2]
        value = fields[3] if len(fields) == 4 else ''
        if label == CORE_LABEL:
            if prop == PROP_INITIALIZE:
                return value.strip() not in ('0', '')
            model.core_properties[prop] = value
        else:
            device = _device(model, label, command)
            (device.properties if initialised else device.pre_init)[prop] = value
    elif command == CMD_LABEL:
        device = _device(model, fields[1], command)
        try:
            state = int(fields[2])
        except ValueError:
            raise _fail(f'Label needs a whole-number state, got {fields[2]!r}') from None
        if state < 0:
            raise _fail(f'Label needs a state >= 0, got {state}')
        device.state_labels[state] = fields[3]
    elif command == CMD_CONFIG_GROUP:
        group = model.config_groups.setdefault(fields[1], {})
        if len(fields) >= 3:
            presets = group.setdefault(fields[2], [])
            if len(fields) >= 5:
                presets.append((fields[3], fields[4], fields[5] if len(fields) == 6 else ''))
    elif command == CMD_CONFIG_PIXEL_SIZE:
        pixel = model.pixel_sizes.setdefault(fields[1], PixelSizeConfig(fields[1]))
        if len(fields) >= 4:
            pixel.settings.append((fields[2], fields[3], fields[4] if len(fields) == 5 else ''))
    elif command == CMD_PIXEL_SIZE_UM:
        pixel = model.pixel_sizes.setdefault(fields[1], PixelSizeConfig(fields[1]))
        try:
            pixel.size_um = float(fields[2])
        except ValueError:
            raise _fail(f'PixelSize_um needs a number, got {fields[2]!r}') from None
    elif command == CMD_DELAY:
        model.delays[fields[1]] = fields[2]
    elif command == CMD_FOCUS_DIRECTION:
        model.focus_directions[fields[1]] = fields[2]
    return initialised


def _device(model: CfgModel, label: str, command: str) -> CfgDevice:
    try:
        return model.devices[label]
    except KeyError:
        known = ', '.join(repr(k) for k in model.devices) or 'none yet'
        raise _fail(f'{command} refers to the device {label!r}, which no Device line before it declares '
                    f'(declared so far: {known})') from None


# ------------------------------------------------------------- classification
def _device_types():
    from pymmcore_plus import DeviceType

    return DeviceType


#: ``--type`` vocabulary -> name of the ``pymmcore_plus.DeviceType`` member.
TYPE_OVERRIDES = {
    'camera': 'Camera', 'stage': 'Stage', 'xystage': 'XYStage', 'shutter': 'Shutter', 'state': 'State',
}


def type_name(device_type) -> str:
    """``'Camera'`` for ``DeviceType.Camera`` (pymmcore-plus names its members ``CameraDevice``)."""
    return device_type.name.removesuffix('Device')


class Classification(dict):
    """``{label: DeviceType or None}`` plus, for every ``None``, why (``reasons``) and where the
    adapters came from (``source``)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reasons: Dict[str, str] = {}
        self.source: str = ''
        #: ``--type`` choices that were applied, label -> type text
        self.overridden: Dict[str, str] = {}


def bare_core(adapter_paths: Sequence[str] = ()):
    """``(core, paths, source)``: a bare ``CMMCorePlus`` that only knows where the adapters are.

    The adapter folder is resolved as the hub does it: explicit paths, then ``MICROMANAGER_PATH``,
    then ``find_micromanager()``. Raises :class:`~._core.MMAdaptersNotFoundError` when there is none.
    """
    resolver = MMCoreHost('converter', adapter_paths=tuple(adapter_paths), logger=_LOG)
    paths, source = resolver._resolve_adapter_paths()
    os.environ[SIGNALS_BACKEND_ENV] = SIGNALS_BACKEND      # no Qt signaler for a throwaway core
    from pymmcore_plus import CMMCorePlus

    return CMMCorePlus(mm_path=paths[0], adapter_paths=list(paths)), list(paths), source


def classify(model: CfgModel, core=None, *, adapter_paths: Sequence[str] = ()) -> Classification:
    """The Micro-Manager device type of every ``.cfg`` device, from the adapter libraries alone.

    ``core`` is a core to ask (nothing needs to be loaded in it); by default a bare one is built with
    :func:`bare_core`. A device whose library cannot be read, or whose device name the library does not
    list, maps to ``None`` with the reason in ``result.reasons``. Nothing is loaded or initialised.
    """
    result = Classification()
    owned = core is None
    if owned:
        try:
            core, paths, source = bare_core(adapter_paths)
        except MMAdaptersNotFoundError as exc:
            for label in model.devices:
                result[label] = None
                result.reasons[label] = f'type unknown, no adapters to ask: {exc}'
            result.source = f'classification unavailable: {exc}'
            return result
        result.source = f'adapters from {source}: {paths}'
    else:
        result.source = 'adapters of the core handed in'
    DeviceType = _device_types()
    try:
        by_library: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[str]]] = {}
        for device in model.devices.values():
            if device.library not in by_library:
                by_library[device.library] = _library_types(core, device.library, DeviceType)
            names, error = by_library[device.library]
            if names is None:
                result[device.label] = None
                result.reasons[device.label] = f'type unknown, adapter library {device.library!r}: {error}'
            elif device.name not in names:
                result[device.label] = None
                result.reasons[device.label] = (
                    f'type unknown, adapter library {device.library!r} has no device {device.name!r} '
                    f'(it offers: {sorted(names)})')
            else:
                result[device.label] = names[device.name]
    finally:
        if owned:
            try:
                core.unloadAllDevices()
            except Exception as exc:                          # noqa: BLE001 - a throwaway core
                _LOG.debug('Unloading the classification core failed: %s', exc)
    return result


def _library_types(core, library: str, DeviceType) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        names = list(core.getAvailableDevices(library))
        types = list(core.getAvailableDeviceTypes(library))
    except Exception as exc:                                  # noqa: BLE001 - MMCore says why in its message
        return None, str(exc)
    return {name: DeviceType(int(kind)) for name, kind in zip(names, types)}, None


def apply_type_overrides(model: CfgModel, types: Classification, overrides: Mapping[str, str]) -> Classification:
    """``types`` with ``--type LABEL=TYPE`` applied; a label the file does not declare, or a type
    outside :data:`TYPE_OVERRIDES`, is a :class:`ValueError` naming the choices."""
    DeviceType = _device_types()
    result = Classification(types)
    result.reasons, result.source = dict(types.reasons), types.source
    result.overridden = dict(getattr(types, 'overridden', {}))
    for label, text in overrides.items():
        if label not in model.devices:
            raise ValueError(f'--type {label}={text}: the .cfg declares no device {label!r} '
                             f'(devices: {list(model.devices)})')
        member = TYPE_OVERRIDES.get(text.lower())
        if member is None:
            raise ValueError(f'--type {label}={text}: the type must be one of {sorted(TYPE_OVERRIDES)}')
        result[label] = getattr(DeviceType, member)
        result.reasons.pop(label, None)
        result.overridden[label] = text.lower()
    return result


# ----------------------------------------------------------------- build_setup
SECTION_HUB, SECTION_DETECTORS, SECTION_POSITIONERS = 'rs232devices', 'detectors', 'positioners'
SECTION_LASERS, SECTION_FLIP_MIRRORS = 'lasers', 'flipMirrors'

MANAGER_HUB = 'micromanager.core'
MANAGER_CAMERA = 'micromanager.camera'
MANAGER_STAGE = 'micromanager.stage'
MANAGER_LIGHT = 'micromanager.light'
MANAGER_FLIP_MIRROR = 'micromanager.flip-mirror'

#: ``axes`` of a Micro-Manager XYStage (the stage manager accepts nothing else) and of a Stage that
#: ``Core,Focus`` names (ImSwitch2's focus axis).
XY_AXES = ['X', 'Y']
FOCUS_AXIS = ['Z']
#: The axis name written for a Stage the file does not make the focus device (see module docstring).
AXIS_PLACEHOLDER = 'TODO'

#: Widgets every generated setup gets (the demo template's set); positioner, laser and flip-mirror
#: widgets are added with their first entry.
BASE_WIDGETS = ['Settings', 'View', 'Recording', 'Image']
SECTION_WIDGETS = ((SECTION_POSITIONERS, 'Positioner'), (SECTION_LASERS, 'Laser'), (SECTION_FLIP_MIRRORS, 'FlipMirror'))

_UNMAPPED = {
    'Hub': "Micro-Manager's own plumbing: peripherals name it as their parent, the hub entry loads it through the .cfg",
    'Serial': 'a serial port other devices use; the .cfg opens it, nothing to map',
    'Core': "Micro-Manager's own core device",
    'Generic': 'a generic device with no ImSwitch2 counterpart',
    'AutoFocus': 'no ImSwitch2 contract for it yet (waits for a concrete user)',
    'SignalIO': "duplicates ImSwitch2's own waveform scanning (NI-DAQ / TriggerScope)",
    'Galvo': "duplicates ImSwitch2's own waveform scanning (NI-DAQ / TriggerScope)",
    'Magnifier': 'no ImSwitch2 counterpart',
    'ImageProcessor': 'no ImSwitch2 counterpart (ImSwitch2 processes frames in ImProcess)',
    'SLM': 'waits for a concrete user',
    'PressurePump': 'no ImSwitch2 counterpart',
    'VolumetricPump': 'no ImSwitch2 counterpart',
    'Unknown': 'MMCore reports no type for it',
    'Any': 'MMCore reports no specific type for it',
}


def _entry(manager: str, hub_name: str, label: str, **properties) -> Dict[str, Any]:
    return {'managerName': manager,
            'managerProperties': {'rs232device': hub_name, 'label': label, **properties}}


def build_setup(model: CfgModel, types: Mapping[str, Any], hub_name: str = 'MMCore',
                config_file: Optional[str] = None, adapter_paths: Sequence[str] = ()
                ) -> Tuple[Dict[str, Any], List[str]]:
    """``(setup, report_lines)`` for ``model`` with the device types ``types``.

    ``setup`` is a plain dict (JSON-serialisable, no placeholder that breaks ``validate-setup``), see
    the module docstring for how a value that cannot be derived is represented. ``config_file`` is
    written to the hub as given (default: the path the model was read from). A device missing from
    ``types`` counts as unresolved.
    """
    DeviceType = _device_types()
    config_file = model.path if config_file is None else config_file
    reasons = getattr(types, 'reasons', {})
    sections: Dict[str, Dict[str, Any]] = {
        SECTION_DETECTORS: {}, SECTION_POSITIONERS: {}, SECTION_LASERS: {}, SECTION_FLIP_MIRRORS: {}}
    written: List[str] = []
    todos: List[str] = []
    skipped: List[str] = []
    notes: List[str] = []
    focus_label = model.roles.get('Focus')
    pixel_names = list(model.pixel_sizes)

    for label, device in model.devices.items():
        device_type = types.get(label)
        what = f'{device.library}/{device.name}'
        if device_type is None:
            reason = reasons.get(label) or 'type unknown (not classified)'
            skipped.append(f'{label!r}  {what}  type unresolved: {reason}')
            continue
        kind = type_name(device_type)
        if device_type == DeviceType.Camera:
            sections[SECTION_DETECTORS][label] = {
                **_entry(MANAGER_CAMERA, hub_name, label), 'forAcquisition': True}
            written.append(f'detectors[{label!r}]  {what}  Camera  -> {MANAGER_CAMERA}')
            if pixel_names:
                notes.append(
                    f'detectors[{label!r}]: cameraPixelSizeUm is not written; Micro-Manager\'s pixel-size '
                    f'config supplies it ({", ".join(pixel_names)}). Set managerProperties.cameraPixelSizeUm '
                    f'to override it.')
            else:
                todos.append(
                    f'detectors[{label!r}]: the .cfg has no pixel-size config, so the pixel size is unknown; '
                    f'set managerProperties.cameraPixelSizeUm (unbinned camera pixel in micrometres).')
        elif device_type in (DeviceType.Stage, DeviceType.XYStage):
            is_xy = device_type == DeviceType.XYStage
            if is_xy:
                axes = list(XY_AXES)
            elif label == focus_label:
                axes = list(FOCUS_AXIS)
            else:
                axes = [AXIS_PLACEHOLDER]
                todos.append(
                    f'positioners[{label!r}]: axes is ["{AXIS_PLACEHOLDER}"]; the .cfg does not make this Stage '
                    f'the focus device (Property,Core,Focus), so the axis name is yours to choose (for '
                    f'example "Z").')
            sections[SECTION_POSITIONERS][label] = {
                **_entry(MANAGER_STAGE, hub_name, label), 'axes': axes, 'forPositioning': True}
            written.append(f'positioners[{label!r}]  {what}  {kind}  -> {MANAGER_STAGE}, axes {axes}')
        elif device_type == DeviceType.Shutter:
            sections[SECTION_LASERS][label] = {
                **_entry(MANAGER_LIGHT, hub_name, label, enable={'kind': 'shutter'}),
                'valueRangeMin': None, 'valueRangeMax': None}
            written.append(f'lasers[{label!r}]  {what}  Shutter  -> {MANAGER_LIGHT}, binary light')
            todos.append(
                f'lasers[{label!r}]: wavelength is not written (Micro-Manager does not know it); set it in '
                f'nanometres. validate-setup warns about the missing wavelength until you do.')
        elif device_type == DeviceType.State:
            count, problem = model.position_count(label)
            if count is None:
                skipped.append(
                    f'{label!r}  {what}  State  position count unknown: {problem}. If it has exactly two '
                    f'positions add a flipMirrors entry by hand ({MANAGER_FLIP_MIRROR}).')
            elif count.count == 2:
                sections[SECTION_FLIP_MIRRORS][label] = _entry(MANAGER_FLIP_MIRROR, hub_name, label)
                written.append(
                    f'flipMirrors[{label!r}]  {what}  State, 2 positions ({count.source})  -> {MANAGER_FLIP_MIRROR}')
            else:
                skipped.append(
                    f'{label!r}  {what}  State, {count.count} positions ({count.source})  not written: '
                    f'multi-position State device (filter wheel, turret, light path); ImSwitch2 has no '
                    f'selector device kind yet, a flip mirror has exactly two positions')
        else:
            reason = _UNMAPPED.get(kind, f'a {kind} device has no ImSwitch2 mapping')
            skipped.append(f'{label!r}  {what}  {kind}  not written: {reason}')

    cameras = list(sections[SECTION_DETECTORS])
    if len(cameras) > 1:
        notes.append(
            f'detectors {cameras}: all are written with forAcquisition true; MMCore has one frame buffer, '
            f'so cameras streaming together must have the same frame size.')
    if relative_path(config_file):
        notes.append(
            f'rs232devices[{hub_name!r}].configFile is the relative path {config_file!r}; MMCore resolves it '
            f'against the folder ImSwitch2 is started from, then the adapter folder. Use an absolute path '
            f'for a setup file you start from elsewhere.')
    summary = _file_summary(model)
    if summary:
        notes.append(summary)
    notes.extend(f'.cfg: {warning}' for warning in model.warnings)

    # ``adapterPaths`` only when given: the hub reads an absent key and an empty list alike
    # (core.py: ``properties.get('adapterPaths') or ()``), and an empty list in a generated
    # file suggests the user was meant to fill it in.
    hub_properties: Dict[str, Any] = {'configFile': config_file}
    if adapter_paths:
        hub_properties['adapterPaths'] = [str(p) for p in adapter_paths]
    hub = {'managerName': MANAGER_HUB, 'managerProperties': hub_properties}
    setup: Dict[str, Any] = {SECTION_HUB: {hub_name: hub}}
    widgets = list(BASE_WIDGETS)
    for section, widget in ((SECTION_DETECTORS, None), *SECTION_WIDGETS):
        if sections[section]:
            setup[section] = sections[section]
            if widget:
                widgets.append(widget)
    setup['availableWidgets'] = widgets

    report = _report(model, types, written, todos, notes, skipped, hub_name)
    return setup, report


def relative_path(path: str) -> bool:
    return not os.path.isabs(os.path.expanduser(str(path)))


def _file_summary(model: CfgModel) -> str:
    parts = []
    if model.config_groups:
        parts.append(f'{len(model.config_groups)} config group(s)')
    if model.pixel_sizes:
        parts.append(f'{len(model.pixel_sizes)} pixel-size config(s)')
    if model.delays:
        parts.append(f'{len(model.delays)} delay(s)')
    if model.focus_directions:
        parts.append(f'{len(model.focus_directions)} focus direction(s)')
    if not parts:
        return ''
    return ('the hub loads the whole .cfg as Micro-Manager does, including ' + ', '.join(parts)
            + '; none of them is a setup entry')


def _report(model, types, written, todos, notes, skipped, hub_name) -> List[str]:
    lines = [f'Micro-Manager configuration {model.path}: {len(model.devices)} device(s)']
    source = getattr(types, 'source', '')
    if source:
        lines.append(f'Types: {source}')
    overridden = getattr(types, 'overridden', {})
    if overridden:
        lines.append('Types given with --type: ' + ', '.join(f'{k}={v}' for k, v in overridden.items()))
    lines.append('')
    lines.append(f'Written to the setup file: hub rs232devices[{hub_name!r}] + {len(written)} device(s)')
    lines.extend(f'  {line}' for line in written)
    lines.append('')
    lines.append(f'To do before the first start ({len(todos)})')
    if todos:
        lines.extend(f'  TODO {line}' for line in todos)
    else:
        lines.append('  nothing the converter knows of')
    if notes:
        lines.append('')
        lines.append('Notes')
        lines.extend(f'  {line}' for line in notes)
    lines.append('')
    lines.append(f'Not written ({len(skipped)})')
    lines.extend(f'  {line}' for line in skipped or ['none'])
    return lines


# ------------------------------------------------------------------------- CLI
def _report_path(out: Path) -> Path:
    stem = out.name[:-len(out.suffix)] if out.suffix else out.name
    return out.with_name(f'{stem}.conversion-report.txt')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m imswitch_device_micromanager',
        description='Micro-Manager device bridge tools for ImSwitch2.')
    sub = parser.add_subparsers(dest='command', required=True)
    convert = sub.add_parser(
        'convert', help='turn a Micro-Manager .cfg into an ImSwitch2 setup-file skeleton',
        description=(
            'Reads CFG with the grammar MMCore parses and writes OUT.json (the setup skeleton) and '
            'OUT.conversion-report.txt (every device that was not written and every value still to '
            'fill in). Device types come from the Micro-Manager adapter libraries; nothing is loaded.'))
    convert.add_argument('cfg', help='the Micro-Manager configuration file')
    convert.add_argument('-o', '--output', required=True, help='the setup file to write (OUT.json)')
    convert.add_argument('--type', action='append', default=[], metavar='LABEL=TYPE',
                         help=f'give or override a device type ({"|".join(TYPE_OVERRIDES)}); repeatable')
    convert.add_argument('--no-classify', action='store_true',
                         help='do not ask the adapter libraries: every device not given by --type is unresolved')
    convert.add_argument('--adapter-path', action='append', default=[], metavar='DIR',
                         help='adapter folder to classify with and to write to the hub as adapterPaths; repeatable '
                              '(default: MICROMANAGER_PATH, then pymmcore-plus\' own search)')
    convert.add_argument('--hub-name', default='MMCore', help='key of the hub in rs232devices (default MMCore)')
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry. Exit code 0 when the setup was written (even with TODOs), 2 when it was not
    (unreadable or malformed ``.cfg``, bad ``--type``, output not writable)."""
    args = _parser().parse_args(argv)
    return _convert(args)


def _convert(args) -> int:
    try:
        model = parse_cfg(args.cfg)
    except (OSError, CfgParseError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    try:
        overrides = _parse_overrides(args.type)
        if args.no_classify:
            types = Classification({label: None for label in model.devices})
            types.reasons = {label: 'type unknown (--no-classify)' for label in model.devices}
            types.source = 'classification skipped (--no-classify)'
        else:
            types = classify(model, adapter_paths=args.adapter_path)
        types = apply_type_overrides(model, types, overrides)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    setup, report = build_setup(model, types, hub_name=args.hub_name, config_file=args.cfg,
                                adapter_paths=args.adapter_path)
    out = Path(args.output)
    report_file = _report_path(out)
    try:
        out.write_text(json.dumps(setup, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        report_file.write_text('\n'.join(report) + '\n', encoding='utf-8')
    except OSError as exc:
        print(f'error: cannot write the output: {exc}', file=sys.stderr)
        return 2
    print('\n'.join(report))
    print(f'\nWrote {out} and {report_file}')
    return 0


def _parse_overrides(items: Iterable[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for item in items:
        label, sep, text = item.rpartition('=')
        if not sep or not label or not text:
            raise ValueError(f'--type {item!r} must look like LABEL=TYPE ({"|".join(TYPE_OVERRIDES)})')
        overrides[label] = text
    return overrides

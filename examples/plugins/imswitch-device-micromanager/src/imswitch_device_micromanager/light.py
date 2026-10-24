"""MicroManagerLightManager: a Micro-Manager light source as an ImSwitch2 laser.

Built on the hub :class:`~.core.MicroManagerCoreManager` and the shared plumbing
:class:`~._bridge.MicroManagerDeviceMixin`; see plan ``micromanager-bridge.md``
sections 4.2, 4.4 (``LaserManager`` row) and 4.7.

Manager properties (``schemas/micromanager_light.schema.json``):

- ``rs232device`` -- name of the ``micromanager.core`` hub in ``rs232devices``
- ``label`` -- the Micro-Manager device that switches emission
- ``adapter`` + ``device`` (+ ``parent``, ``preInitProperties``) -- *inline mode*
  for that device (see :class:`~._bridge.MicroManagerDeviceMixin`)
- ``enable`` -- how emission is switched, one of

  - ``{"kind": "shutter"}``: ``label`` is a Shutter (``setShutterOpen``);
  - ``{"kind": "state", "on": <label or index>, "off": <label or index>}``:
    ``label`` is a State device (``setState``), for example a filter-wheel
    position or LED selector with a "Closed" position;
  - ``{"kind": "property", "name": ..., "on": ..., "off": ...}``: a property of
    ``label`` takes one of two values (``setProperty``).

- ``power`` (optional) -- ``{"label"?, "property", "units", "decimals"}``: a
  numeric property that sets the output. Without it the light is *binary*
  (``isBinary``): the widget shows an on/off switch and no power field.

Rules.

- Nothing here guesses a unit or a precision. Micro-Manager properties carry
  neither, so ``units`` and ``decimals`` are required whenever ``power`` is set.
- ``LaserInfo.valueRangeMin`` / ``valueRangeMax`` are the setup's safe range for
  the power property. When Micro-Manager states limits for the property, the
  setup range must lie inside them (both ranges are in the refusal); a value
  outside the setup range is refused even when Micro-Manager would take it.
- Every command is read back. A write that Micro-Manager accepts but the device
  does not show (a read-only property, an adapter that ignores it) is a failed
  command, not a successful one.
- Emission and power are separate switches in Micro-Manager: ``setValue`` writes
  the power property and does not switch emission, whatever ``enabled`` says.
- ``for_scanning`` is ignored: the bridge has no TTL path, so a scan cannot gate
  this light (the scan module's TTL device list decides who emits during a scan).
- Modulation is unsupported (``isModulated`` False).

Failure convention. ``setEnabled`` and ``setValue`` log a failed command and
return ``False`` (the laser controller then shows the light off); a light that
is not connected raises ``DeviceNotConnectedError``, which the controller
handles. ``applyEnabled``, ``applyValue`` and ``applyRawDrive`` raise
:class:`RawDriveError` instead. The reason of the last failure is
:attr:`lastError`. A refused command does not change the connection status: a
status of ERROR would make the manager refuse the next ``setEnabled(False)``.
"""
from __future__ import annotations

import difflib
import math
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple, Union

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import DeviceNotConnectedError
from imswitch.imcontrol.model.managers.lasers.LaserManager import RawDriveError
from imswitch.pluginapi import LaserManager

from ._bridge import MicroManagerDeviceMixin

#: ``valueUnits`` / ``valueDecimals`` of a binary light. The laser widget draws no value
#: field for a binary laser, and ``LaserManager`` needs a string and an integer; an empty
#: unit and zero decimals invent nothing (a made-up "mW" would be a claim about hardware).
BINARY_UNITS = ''
BINARY_DECIMALS = 0

KIND_SHUTTER = 'shutter'
KIND_STATE = 'state'
KIND_PROPERTY = 'property'
ENABLE_KINDS = (KIND_SHUTTER, KIND_STATE, KIND_PROPERTY)

#: Width of the read-back tolerance, in units of the last printed digit. A value sent with
#: ``decimals`` decimals is stored by the device to at least that precision, so the read-back
#: may differ from the request by half of the last digit and no more.
READBACK_TOLERANCE_ULP = 0.5


@dataclass(frozen=True)
class _EnableSpec:
    """The ``enable`` block as configured (``on``/``off`` unresolved)."""

    kind: str
    on: Any = None
    off: Any = None
    name: Optional[str] = None            # property kind only


@dataclass(frozen=True)
class _PowerSpec:
    label: str
    property: str
    units: str
    decimals: int


@dataclass
class _Resolved:
    """What the enable block resolved to on the loaded device: values to write and compare."""

    on: Any
    off: Any


@dataclass(frozen=True)
class _PropertyKind:
    numeric: bool
    integer: bool


def _same_value(a: Any, b: Any) -> bool:
    """Property values as Micro-Manager shows them: equal text, or equal numbers ('1' and '1.0000')."""
    if str(a) == str(b):
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class MicroManagerLightManager(MicroManagerDeviceMixin, LaserManager):
    """A Micro-Manager light source (shutter, state device or property) as a laser."""

    BRIDGE_MANAGER_ID = 'micromanager.light'
    BRIDGE_CATEGORY = 'laser'
    BRIDGE_DEVICE_NOUN = 'light'

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        properties = dict(laserInfo.managerProperties or {})
        self._bridge_identify(properties, name, self.__logger)
        self._enable = self._parse_enable(properties.get('enable'))
        self._power = self._parse_power(properties.get('power'), laserInfo)
        self._bridge_attach(properties, lowLevelManagers)
        self._resolved: Optional[_Resolved] = None
        self._model = ''
        self._ready = False
        self._last_error: Optional[str] = None

        power = self._power
        super().__init__(
            laserInfo, name, isBinary=power is None,
            valueUnits=power.units if power else BINARY_UNITS,
            valueDecimals=power.decimals if power else BINARY_DECIMALS)

        self._bridge_install_backend(laserInfo)
        try:
            model = self._bridge_adopt(self._adopt)
        except BaseException:
            self._unload_inline()            # a setup error must not leave the device loaded in the core
            raise
        if model is not None:
            self._model = model
            self._ready = True
            self._setConnected(f'{model}: {self._label} connected')

    # ------------------------------------------------------------------ setup
    def _parse_enable(self, enable) -> _EnableSpec:
        """The ``enable`` block's shape; the device-dependent checks follow at adoption."""
        where = f"laser {self._setupName!r}: managerProperties.enable"
        if not isinstance(enable, dict):
            raise ValueError(
                f"{where} is required and must be an object with a 'kind' of {list(ENABLE_KINDS)}")
        kind = enable.get('kind')
        if kind not in ENABLE_KINDS:
            raise ValueError(f"{where}.kind is {kind!r}; it must be one of {list(ENABLE_KINDS)}")
        allowed = {'kind'} if kind == KIND_SHUTTER else {'kind', 'on', 'off'} | ({'name'} if kind == KIND_PROPERTY else set())
        extra = sorted(set(enable) - allowed)
        if extra:
            raise ValueError(f"{where} of kind {kind!r} does not take {extra}; it takes {sorted(allowed)}")
        if kind == KIND_SHUTTER:
            return _EnableSpec(kind)
        for key in ('on', 'off'):
            value = enable.get(key)
            valid = (isinstance(value, str) or _is_number(value)) if kind == KIND_PROPERTY \
                else (isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)))
            if not valid or value == '':
                raise ValueError(
                    f"{where}.{key} must be a "
                    + ("string or a number" if kind == KIND_PROPERTY else "state label (string) or state index (integer)")
                    + f", got {value!r}")
        name = None
        if kind == KIND_PROPERTY:
            name = enable.get('name')
            if not isinstance(name, str) or not name:
                raise ValueError(f"{where}.name must be the property to write (a non-empty string), got {name!r}")
        return _EnableSpec(kind, enable['on'], enable['off'], name)

    def _parse_power(self, power, laserInfo) -> Optional[_PowerSpec]:
        if power is None:
            return None
        where = f"laser {self._setupName!r}: managerProperties.power"
        if not isinstance(power, dict):
            raise ValueError(f"{where} must be an object with 'property', 'units' and 'decimals'")
        extra = sorted(set(power) - {'label', 'property', 'units', 'decimals'})
        if extra:
            raise ValueError(f"{where} does not take {extra}; it takes label, property, units, decimals")
        label = power.get('label', self._label)
        prop, units, decimals = power.get('property'), power.get('units'), power.get('decimals')
        if not isinstance(label, str) or not label:
            raise ValueError(f"{where}.label must be a device label (a non-empty string), got {label!r}")
        if not isinstance(prop, str) or not prop:
            raise ValueError(f"{where}.property must be the numeric property to write (a non-empty string), got {prop!r}")
        if not isinstance(units, str) or not units:
            raise ValueError(
                f"{where}.units is required (for example 'mW', 'V' or '%'): Micro-Manager properties carry no "
                f"unit and the bridge does not guess one")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
            raise ValueError(
                f"{where}.decimals is required and must be a whole number >= 0 (how many decimals the power "
                f"value has): Micro-Manager properties carry no precision and the bridge does not guess one")
        lo, hi = laserInfo.valueRangeMin, laserInfo.valueRangeMax
        if not _is_number(lo) or not _is_number(hi) or not hi > lo:
            raise ValueError(
                f"laser {self._setupName!r}: with managerProperties.power the setup entry needs numeric "
                f"valueRangeMin < valueRangeMax (the safe range of {prop!r} in {units}), got "
                f"valueRangeMin={lo!r}, valueRangeMax={hi!r}")
        return _PowerSpec(label, prop, units, decimals)

    # --------------------------------------------------------------- adoption
    def _expected_device_types(self) -> Tuple[Any, ...]:
        from pymmcore_plus import DeviceType

        return {KIND_SHUTTER: (DeviceType.Shutter,), KIND_STATE: (DeviceType.State,)}.get(self._enable.kind, ())

    def _expected_type_text(self) -> str:
        return {KIND_SHUTTER: 'a Shutter', KIND_STATE: 'a State device'}[self._enable.kind] \
            + f" (managerProperties.enable.kind is {self._enable.kind!r})"

    def _missing_message(self, label: str) -> str:
        host = self._host
        types = self._expected_device_types()
        if types:
            return host.missing_device_message(label, types[0], self._enable.kind)
        loaded = list(host.call('getLoadedDevices'))
        near = difflib.get_close_matches(label, loaded, n=1)
        hint = f" Did you mean {near[0]!r}?" if near else ''
        return f"Micro-Manager has no device {label!r}.{hint} Loaded devices: {loaded}"

    def _resolve_label(self):
        with self._host.lock:
            self._ensure_loaded()
            if self._enable.kind != KIND_PROPERTY:
                self._expect_device_type()

    def _adopt(self) -> str:
        """Resolve the label, check the enable block and the power property against the device;
        the model text. An inline device loaded by this call is unloaded again when anything fails."""
        loaded_before = self._inline_loaded
        try:
            with self._host.lock:
                self._resolve_label()
                core = self._host.core
                self._resolved = self._resolve_enable(core)
                self._check_power(core)
                return f'{core.getDeviceLibrary(self._label)}/{core.getDeviceName(self._label)}'
        except BaseException:
            if self._inline_loaded and not loaded_before:
                self._unload_inline()
            raise

    def _where(self) -> str:
        return f"laser {self._setupName!r}: Micro-Manager device {self._label!r}"

    def _resolve_enable(self, core) -> _Resolved:
        spec = self._enable
        if spec.kind == KIND_SHUTTER:
            return _Resolved(True, False)
        if spec.kind == KIND_STATE:
            on, off = self._state_index(core, spec.on, 'on'), self._state_index(core, spec.off, 'off')
            if on == off:
                raise ValueError(
                    f"{self._where()}: enable.on {spec.on!r} and enable.off {spec.off!r} are both state {on}; "
                    f"they must differ")
            return _Resolved(on, off)
        on = self._property_text(core, self._label, spec.name, spec.on, 'on')
        off = self._property_text(core, self._label, spec.name, spec.off, 'off')
        if _same_value(on, off):
            raise ValueError(f"{self._where()}: enable.on and enable.off are both {on!r}; they must differ")
        return _Resolved(on, off)

    def _state_index(self, core, ref: Union[str, int], which: str) -> int:
        """The index of state ``ref`` (a label or an index) of the label's State device."""
        count = int(core.getNumberOfStates(self._label))
        labels = list(core.getStateLabels(self._label))
        if isinstance(ref, str):
            if ref not in labels:
                near = difflib.get_close_matches(ref, labels, n=1)
                hint = f" Did you mean {near[0]!r}?" if near else ''
                raise ValueError(
                    f"{self._where()} has no state labelled {ref!r} (enable.{which}).{hint} "
                    f"Its state labels are {labels}")
            index = int(core.getStateFromLabel(self._label, ref))
        else:
            index = int(ref)
            if not 0 <= index < count:
                raise ValueError(
                    f"{self._where()} has {count} states (0 to {count - 1}); enable.{which} is {index}. "
                    f"Its state labels are {labels}")
        return index

    def _property_text(self, core, label: str, name: str, value: Any, which: str) -> str:
        """The text to write for ``value`` of a property, checked against what the property allows."""
        prop = self._require_property(core, label, name)
        if core.isPropertyReadOnly(label, name):
            raise ValueError(f"laser {self._setupName!r}: property {name!r} of {label!r} is read-only")
        allowed = list(core.getAllowedPropertyValues(label, name))
        if allowed:
            for text in allowed:
                if _same_value(text, value):
                    return str(text)
            raise ValueError(
                f"laser {self._setupName!r}: {value!r} (enable.{which}) is not an allowed value of property "
                f"{name!r} of {label!r}; allowed: {allowed}")
        if prop.numeric and core.hasPropertyLimits(label, name):
            lo, hi = float(core.getPropertyLowerLimit(label, name)), float(core.getPropertyUpperLimit(label, name))
            try:
                inside = lo <= float(value) <= hi
            except (TypeError, ValueError):
                inside = False
            if not inside:
                raise ValueError(
                    f"laser {self._setupName!r}: {value!r} (enable.{which}) is outside the limits [{lo:g}, {hi:g}] "
                    f"of property {name!r} of {label!r}")
        return str(value)

    def _require_property(self, core, label: str, name: str) -> _PropertyKind:
        from pymmcore_plus import PropertyType

        if label not in core.getLoadedDevices():
            raise ValueError(
                f"laser {self._setupName!r}: Micro-Manager has no device {label!r}. "
                f"Loaded devices: {list(core.getLoadedDevices())}")
        names = list(core.getDevicePropertyNames(label))
        if name not in names:
            near = difflib.get_close_matches(name, names, n=1)
            hint = f" Did you mean {near[0]!r}?" if near else ''
            raise ValueError(f"laser {self._setupName!r}: device {label!r} has no property {name!r}.{hint} Its properties: {names}")
        kind = core.getPropertyType(label, name)
        return _PropertyKind(numeric=kind in (PropertyType.Float, PropertyType.Integer), integer=kind == PropertyType.Integer)

    def _check_power(self, core) -> None:
        power = self._power
        if power is None:
            return
        prop = self._require_property(core, power.label, power.property)
        where = f"laser {self._setupName!r}: power property {power.property!r} of {power.label!r}"
        if not prop.numeric:
            raise ValueError(f"{where} is not numeric (Micro-Manager type {core.getPropertyType(power.label, power.property)!s})")
        if core.isPropertyReadOnly(power.label, power.property):
            raise ValueError(f"{where} is read-only")
        if prop.integer and power.decimals != 0:
            raise ValueError(f"{where} is an integer property; power.decimals must be 0, got {power.decimals}")
        if core.hasPropertyLimits(power.label, power.property):
            lo, hi = float(core.getPropertyLowerLimit(power.label, power.property)), \
                float(core.getPropertyUpperLimit(power.label, power.property))
            if self.valueRangeMin < lo or self.valueRangeMax > hi:
                raise ValueError(
                    f"{where} accepts [{lo:g}, {hi:g}] in Micro-Manager, but the setup range "
                    f"valueRangeMin/valueRangeMax is [{self.valueRangeMin:g}, {self.valueRangeMax:g}]; "
                    f"narrow the setup range to lie inside Micro-Manager's")

    # ------------------------------------------------------------- properties
    @property
    def lastError(self) -> Optional[str]:
        """Why the last command failed (``None`` after a command that succeeded)."""
        return self._last_error

    @property
    def supportsRawDrive(self) -> bool:
        """The checked commands drive the power property, so they exist only with ``power``."""
        return self._power is not None

    # --------------------------------------------------------- hardware access
    def _read_enabled(self, core) -> Optional[bool]:
        """Whether the device shows emission on (True), off (False), or neither (None). Hub lock held."""
        spec, resolved, label = self._enable, self._resolved, self._label
        if spec.kind == KIND_SHUTTER:
            return bool(core.getShutterOpen(label))
        if spec.kind == KIND_STATE:
            state = int(core.getState(label))
            return True if state == resolved.on else False if state == resolved.off else None
        text = core.getProperty(label, spec.name)
        return True if _same_value(text, resolved.on) else False if _same_value(text, resolved.off) else None

    def _switch(self, enabled: bool) -> None:
        """Switch emission and read it back; raises when the device does not show the new state."""
        host, label = self._use('switch emission')
        spec, resolved = self._enable, self._resolved
        with host.lock:
            core = host.core
            target = resolved.on if enabled else resolved.off
            if spec.kind == KIND_SHUTTER:
                core.setShutterOpen(label, bool(enabled))
            elif spec.kind == KIND_STATE:
                core.setState(label, int(target))
            else:
                core.setProperty(label, spec.name, target)
            shown = self._read_enabled(core)
        if shown is not bool(enabled):
            raise RuntimeError(
                f"{label!r} was told to switch {'on' if enabled else 'off'} but shows "
                f"{'nothing definite' if shown is None else 'on' if shown else 'off'}")

    def _format_power(self, value: Union[int, float]) -> Tuple[float, str]:
        """``(value, text)`` of a power request: numeric, inside the setup range, printed with ``decimals``."""
        if not _is_number(value) or not math.isfinite(value):
            raise ValueError(f"power {value!r} is not a finite number")
        if not self.valueRangeMin <= value <= self.valueRangeMax:
            raise ValueError(
                f"power {value:g} {self._power.units} is outside the setup range "
                f"[{self.valueRangeMin:g}, {self.valueRangeMax:g}] {self._power.units}")
        return float(value), f'{value:.{self._power.decimals}f}'

    def _write_power(self, value: Union[int, float]) -> float:
        """Write the power property and return what the device shows afterwards."""
        if self._power is None:
            raise RuntimeError(f"laser {self._setupName!r} has no power property (managerProperties.power); it is binary")
        host, _ = self._use('set power')
        requested, text = self._format_power(value)
        power = self._power
        with host.lock:
            core = host.core
            core.setProperty(power.label, power.property, text)
            applied = float(core.getProperty(power.label, power.property))
        tolerance = READBACK_TOLERANCE_ULP * 10.0 ** -power.decimals
        if abs(applied - float(text)) > tolerance:
            self.__logger.warning(
                '%s: asked for %s %s, the device shows %g', self._setupName, text, power.units, applied)
        return applied

    # ------------------------------------------------------------------ laser
    def setEnabled(self, enabled):
        """Switch emission; ``True`` when the device shows the new state, else ``False`` (logged)."""
        try:
            self._switch(bool(enabled))
        except DeviceNotConnectedError:
            raise
        except Exception as exc:                          # noqa: BLE001 - the laser convention: log, return False
            self._last_error = f'{type(exc).__name__}: {exc}'
            self.__logger.error('Light %s: switching %s failed: %s', self._setupName, 'on' if enabled else 'off', exc)
            return False
        self._last_error = None
        return True

    def setValue(self, value, enabled=True, for_scanning=False):
        """Write the power property (see the module docstring for ``enabled`` and ``for_scanning``);
        ``True`` when written, else ``False`` (logged)."""
        try:
            self._write_power(value)
        except DeviceNotConnectedError:
            raise
        except Exception as exc:                          # noqa: BLE001 - the laser convention: log, return False
            self._last_error = f'{type(exc).__name__}: {exc}'
            self.__logger.error('Light %s: setting power %r failed: %s', self._setupName, value, exc)
            return False
        self._last_error = None
        return True

    def applyRawDrive(self, value):
        """The power property, checked: returns the value the device shows, raises
        :class:`RawDriveError` on any failure (there is no calibration lookup to bypass)."""
        return self._checked(lambda: self._write_power(value), f'power {value!r}')

    def applyValue(self, value):
        """Same as :meth:`applyRawDrive`: there is no calibration between a value and the property."""
        return self.applyRawDrive(value)

    def applyEnabled(self, enabled):
        """Switch emission, checked: ``True`` when the device shows it, :class:`RawDriveError` otherwise."""
        self._checked(lambda: self._switch(bool(enabled)), f'switching {"on" if enabled else "off"}')
        return True

    def _checked(self, operation: Callable[[], Any], what: str):
        try:
            result = operation()
        except Exception as exc:                          # noqa: BLE001 - re-raised as the checked-command error
            self._last_error = f'{type(exc).__name__}: {exc}'
            raise RawDriveError(f'{self._setupName}: {what} failed: {exc}') from exc
        self._last_error = None
        return result

    # --------------------------------------------------------------- lifecycle
    def _lifecycleSafeState(self, *, verified: bool):
        """Emission off before the hub goes away; with ``verified`` the read-back must say so."""
        if not (self._ready and self._host.is_connected):
            return []
        try:
            self._switch(False)
        except Exception as exc:                          # noqa: BLE001
            return [f'emission could not be verified off: {exc}'] if verified else []
        return []

    def _reconnected_summary(self) -> str:
        return f'{self._model}: {self._label} reconnected; emission off'

    def _reinitialise(self) -> List[str]:
        """Adopt the (re)loaded device (re-load an inline one, check the enable block and the power
        property again), then switch emission off and verify that it reads back off (plan 4.7)."""
        try:
            self._model = self._adopt()
            self._ready = True
            self._switch(False)
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            return [f'{type(exc).__name__}: {exc}']
        return []

    def finalize(self):
        """Emission off, unload an inline device, then the base class."""
        if self._ready and self._host.is_connected:
            try:
                self._switch(False)
            except Exception as exc:                      # noqa: BLE001 - finalizing must go on
                self.__logger.error('Switching %s off at finalize failed: %s', self._label, exc)
        self._unload_inline()
        super().finalize()
        self._setFinalizedStatus()

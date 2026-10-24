"""MicroManagerStageManager: one Micro-Manager Stage or XYStage as an ImSwitch2 positioner.

Built on the hub :class:`~.core.MicroManagerCoreManager` (``.host`` is the
:class:`~._core.MMCoreHost`); see plan ``micromanager-bridge.md`` sections 3
(Homing), 4.2 to 4.4 and 4.7. The camera manager (``camera.py``) is the model
for hub lookup, inline loading, status and reconnect.

Manager properties (``schemas/micromanager_stage.schema.json``):

- ``rs232device`` -- name of the ``micromanager.core`` hub in ``rs232devices``
- ``label`` -- the Micro-Manager device label
- ``adapter`` + ``device`` (+ ``parent``, ``preInitProperties``) -- *inline
  mode*: the manager loads the device itself and unloads it on finalize;
  without them the label must already be loaded by the hub's ``.cfg``
  (*system-config mode*)
- ``waitForDevice`` (default true) -- block until a move completes
- ``homeable`` (default false) -- the setup declares that the adapter supports
  a home command and that the reference action may use it

Axes. A Micro-Manager ``Stage`` has one axis (any name in the setup's
``axes``, for example ``["Z"]``); an ``XYStage`` has exactly ``["X", "Y"]``.
Both systems use micrometres, so nothing is scaled.

Position. The tracked position is what the hardware says: ``initialPosition``
is read from the device, a blocking move reads the position back after the wait
and records that, and ``stagePositionChanged`` / ``XYStagePositionChanged``
events update it from MMCore's callback thread. The handlers only store, under
a lock of this manager's own: they must not take the hub lock, which a move
holds while it waits for the device (the callback would then wait for the very
move that waits for it).

Homing. ``home`` moves hardware and MMCore has no query for whether an adapter
supports it, so it is called from :meth:`MicroManagerStageManager.reference`
and nowhere else. ``requiresReference`` is a class attribute of
``PositionerManager``; this manager sets it per instance from ``homeable``.
"""
from __future__ import annotations

import difflib
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import rs232BackedPrimarySpec
from imswitch.imcontrol.model.devices.status import (
    DeviceFailureKind, DeviceNotConnectedError, DeviceRuntimeMode, backend_attribute,
)
from imswitch.pluginapi import PositionerManager

from ._core import MMCoreHostError
from .core import MicroManagerCoreManager

#: Axis names of a Micro-Manager XYStage, in the order ``getXYPosition`` returns them.
XY_AXES = ['X', 'Y']

#: Position of a stage whose device is not there yet (hub down at startup, transient
#: device not connected). Not a measurement: "unknown", so nothing can mistake it for
#: a position (a zero would be one). Replaced by the device's own position on adoption.
UNKNOWN_POSITION = float('nan')

#: ``model`` text of a stage whose device is not there yet.
ABSENT_MODEL = 'Micro-Manager stage (not connected)'

#: How often a blocking move asks the device whether it is still busy. MMCore's own
#: ``waitForDevice`` polls ``Busy()`` every 10 ms (``CMMCore::pollingIntervalMs_``);
#: the same cadence here, but between polls the hub lock is RELEASED, so a stage move
#: that takes seconds does not stall the camera drain thread (plan section 4.5: a stalled
#: drain overflows the circular buffer and fails the recording).
BUSY_POLL_S = 0.010


@dataclass
class _Binding:
    """What the backend holder keeps as "the connected backend": the stage's label on the host."""

    label: str
    host: Any


@dataclass
class _Adopted:
    """Everything read from Micro-Manager when the device is adopted."""

    is_xy: bool
    model: str
    position: Dict[str, float]


class MicroManagerStageManager(PositionerManager):
    """A Micro-Manager Stage (one axis) or XYStage (X and Y) as a positioner."""

    #: The backend holder's current backend: the :class:`_Binding`, or the absent stand-in
    #: that raises ``DeviceNotConnectedError`` on any use.
    _binding = backend_attribute()

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        properties = dict(positionerInfo.managerProperties or {})
        self._props = properties
        self._setupName = name
        self._label = self._required_text(properties, 'label')
        if positionerInfo.forScanning:
            raise ValueError(
                f"positioner {name!r}: forScanning must be false for a Micro-Manager stage; Micro-Manager "
                f"stages have no analog scan line (scanning stays with ImSwitch2's own waveform scanners)")
        self._axes = list(positionerInfo.axes)    # the base class only has them after its __init__
        self._check_axes_shape(self._axes)
        self._hub = self._find_hub(properties, lowLevelManagers)
        self._host = self._hub.host
        self._inline = self._parse_inline(properties)
        self._inline_loaded = False
        self._wait_for_device = self._bool_property(properties, 'waitForDevice', True)
        self._homeable = self._bool_property(properties, 'homeable', False)
        #: Per instance, from the setup: the class attribute of PositionerManager stays False.
        #: Must be set before PositionerManager.__init__, which derives the reference state from it.
        self.requiresReference = self._homeable

        # Guards the tracked position against the callback thread of MMCore's events (see module doc).
        self._track_lock = threading.Lock()
        self._idle_poll = threading.Event()        # never set: a sleep that other code could wake
        self._ready = False                       # False until the device was adopted
        self._is_xy = self._axes == XY_AXES
        self._model = ABSENT_MODEL
        self._events_core = None                  # the core the event handlers are connected to

        # Before the first status write the mixin's getattr default is enough, but the holder's
        # failure path reads the runtime mode: give it a value (same as the camera manager).
        self.__dict__['_deviceRuntimeMode'] = DeviceRuntimeMode.REAL
        self._installBackend(
            self._open_binding, label=f'Micro-Manager stage {self._label}',
            transient=bool(positionerInfo.transient), connect_on_startup=bool(positionerInfo.connectOnStartup),
            close=lambda binding: None)
        adopted: Optional[_Adopted] = None
        try:
            if self.backendIsReal:
                adopted = self._adopt()
            elif self.runtimeMode is not DeviceRuntimeMode.ABSENT:
                self._mirror_hub_status()
            initial = adopted.position if adopted else {axis: UNKNOWN_POSITION for axis in self._axes}
            super().__init__(positionerInfo, name, initialPosition=initial)
        except BaseException:
            self._unload_inline()            # a setup error must not leave the device loaded in the core
            raise

        if adopted is not None:
            self._is_xy = adopted.is_xy
            self._model = adopted.model
            self._ready = True
            self._connect_events()
            self._setConnected(f'{self._model}: {self._label} connected')

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _required_text(properties, key) -> str:
        value = properties.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"micromanager.stage needs managerProperties.{key} (a non-empty string)")
        return value

    def _bool_property(self, properties, key, default: bool) -> bool:
        value = properties.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(
                f"positioner {self._setupName!r}: managerProperties.{key} must be true or false, got {value!r}")
        return value

    def _check_axes_shape(self, axes: List[str]) -> None:
        """A Stage has one axis, an XYStage exactly X and Y: nothing else can ever be valid."""
        if len(axes) == 1 or axes == XY_AXES:
            return
        raise ValueError(
            f"positioner {self._setupName!r}: axes {axes} do not fit a Micro-Manager stage; "
            f"a Stage needs exactly one axis (any name, for example ['Z']) and an XYStage exactly {XY_AXES}")

    def _find_hub(self, properties, lowLevelManagers) -> MicroManagerCoreManager:
        hub_name = self._required_text(properties, 'rs232device')
        hubs = lowLevelManagers.get('rs232sManager')
        try:
            hub = hubs[hub_name]
        except (KeyError, TypeError, IndexError):
            raise ValueError(
                f"positioner {self._setupName!r}: managerProperties.rs232device is {hub_name!r}, but no "
                f"rs232devices entry of that name exists; declare the Micro-Manager hub there "
                f"(managerName 'micromanager.core')") from None
        if not isinstance(hub, MicroManagerCoreManager):
            raise ValueError(
                f"positioner {self._setupName!r}: rs232devices[{hub_name!r}] is a {type(hub).__name__}, not a "
                f"Micro-Manager hub; managerProperties.rs232device must name an entry with "
                f"managerName 'micromanager.core'")
        return hub

    def _parse_inline(self, properties):
        """``(adapter, device, parent, preInit)`` in inline mode, else ``None``."""
        adapter, device = properties.get('adapter'), properties.get('device')
        if bool(adapter) != bool(device):
            raise ValueError(
                f"positioner {self._setupName!r}: inline mode needs both managerProperties.adapter and "
                f"managerProperties.device (got adapter={adapter!r}, device={device!r})")
        if not adapter:
            return None
        return adapter, device, properties.get('parent') or None, dict(properties.get('preInitProperties') or {})

    def _open_binding(self) -> _Binding:
        """``open_real`` of the backend holder: the hub must be connected."""
        if not self._hub.isConnected:
            details = self._hub.connectionStatusDetails or self._hub.connectionStatusSummary
            raise DeviceNotConnectedError(
                f"Micro-Manager core {self._hub.name!r} is not connected"
                + (f": {details}" if details else ''))
        return _Binding(self._label, self._host)

    def _mirror_hub_status(self) -> None:
        """Carry the hub's failure (kind and message) as this stage's status."""
        hub = self._hub
        details = hub.connectionStatusDetails or hub.connectionStatusSummary or 'not connected'
        summary = f'{self._label}: Micro-Manager core {hub.name} is not connected'
        if hub.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND:
            self._setDeviceNotFound(details, summary=summary)
        else:
            self._setConnectionError(details, summary=summary)

    def _missing_message(self, label: str) -> str:
        """The "did you mean" error for a label the core has no stage under."""
        from pymmcore_plus import DeviceType

        host = self._host
        stages = host.labels_of_type(DeviceType.Stage)
        xy_stages = host.labels_of_type(DeviceType.XYStage)
        near = difflib.get_close_matches(label, stages + xy_stages, n=1)
        hint = f" Did you mean {near[0]!r}?" if near else ''
        return (f"Micro-Manager has no Stage or XYStage device {label!r}.{hint} "
                f"Loaded Stage devices: {stages}; loaded XYStage devices: {xy_stages}")

    def _resolve_label(self):
        """Make sure the label is a loaded Stage or XYStage that fits ``axes`` (inline mode loads it)."""
        from pymmcore_plus import DeviceType

        host, label = self._host, self._label
        with host.lock:
            loaded = host.has_device(label)
            if loaded and self._inline is not None and not self._inline_loaded:
                raise ValueError(
                    f"positioner {self._setupName!r}: Micro-Manager device {label!r} is already loaded by the "
                    f"hub's configuration, so managerProperties.adapter/device would load it a second "
                    f"time; remove adapter/device (system-config mode) or use another label")
            if not loaded:
                if self._inline is None:
                    raise ValueError(
                        f"positioner {self._setupName!r} (managerProperties.label): "
                        + self._missing_message(label) + "; or give adapter and device to load it inline")
                adapter, device, parent, pre_init = self._inline
                host.load_device(label, adapter, device, parent, pre_init)
                self._inline_loaded = True
            device_type = host.device_type(label)
            axes = self._axes
            if device_type == DeviceType.Stage:
                if len(axes) != 1:
                    raise ValueError(
                        f"positioner {self._setupName!r}: Micro-Manager device {label!r} is a Stage (one axis), "
                        f"but axes is {axes}; give it exactly one axis, for example ['Z']")
                return False
            if device_type == DeviceType.XYStage:
                if axes != XY_AXES:
                    raise ValueError(
                        f"positioner {self._setupName!r}: Micro-Manager device {label!r} is an XYStage, which "
                        f"needs axes {XY_AXES}, but axes is {axes}")
                return True
            raise ValueError(
                f"positioner {self._setupName!r}: Micro-Manager device {label!r} is a "
                f"{device_type.name.removesuffix('Device')}, not a Stage or XYStage")

    def _unload_inline(self) -> None:
        if not self._inline_loaded:
            return
        self._inline_loaded = False
        try:
            if self._host.is_connected and self._host.has_device(self._label):
                self._host.unload_device(self._label)
        except Exception as exc:                      # noqa: BLE001 - the caller is already failing or finalizing
            self.__logger.warning('Unloading inline stage %s failed: %s', self._label, exc)

    def _adopt(self) -> _Adopted:
        """Resolve the label and read the device's identity and position; an inline device loaded
        by this call is unloaded again when anything fails."""
        loaded_before = self._inline_loaded
        try:
            with self._host.lock:
                is_xy = self._resolve_label()
                core = self._host.core
                return _Adopted(
                    is_xy=is_xy, model=f'{core.getDeviceLibrary(self._label)}/{core.getDeviceName(self._label)}',
                    position=self._read_position(core, is_xy))
        except BaseException:
            if self._inline_loaded and not loaded_before:
                self._unload_inline()
            raise

    def _read_position(self, core, is_xy: Optional[bool] = None) -> Dict[str, float]:
        """``{axis: position}`` from the device, in micrometres. Hub lock held."""
        is_xy = self._is_xy if is_xy is None else is_xy
        axes = self._axes
        if is_xy:
            x, y = core.getXYPosition(self._label)
            return {axes[0]: float(x), axes[1]: float(y)}
        return {axes[0]: float(core.getPosition(self._label))}

    # ------------------------------------------------------------- properties
    @property
    def label(self) -> str:
        """The Micro-Manager device label."""
        return self._label

    @property
    def hub(self) -> MicroManagerCoreManager:
        return self._hub

    @property
    def homeable(self) -> bool:
        """Whether the setup declares that the adapter can home and the reference action may do so."""
        return self._homeable

    def getDeviceDescriptorSpec(self):
        return rs232BackedPrimarySpec(
            category='positioner', rs232_name=str(self._props['rs232device']),
            display_name=f'{self._setupName} (Micro-Manager {self._label})')

    # ----------------------------------------------------------------- events
    def _connect_events(self) -> None:
        """Follow the device's own position notifications (a joystick or a front panel moves it too)."""
        with self._host.lock:
            self._disconnect_events()
            core = self._host.core
            signal = core.events.XYStagePositionChanged if self._is_xy else core.events.stagePositionChanged
            signal.connect(self._on_xy_event if self._is_xy else self._on_stage_event)
            self._events_core = core

    def _disconnect_events(self) -> None:
        core, self._events_core = self._events_core, None
        if core is None:
            return
        for signal_name, handler in (('stagePositionChanged', self._on_stage_event),
                                     ('XYStagePositionChanged', self._on_xy_event)):
            try:
                getattr(core.events, signal_name).disconnect(handler)
            except Exception:                          # noqa: BLE001 - not connected, or the core is already gone
                pass

    def _on_stage_event(self, name, position) -> None:
        """MMCore's callback thread: store only (never the hub lock, never Qt)."""
        if name == self._label:
            self._store_position({self.axes[0]: position})

    def _on_xy_event(self, name, x, y) -> None:
        """MMCore's callback thread: store only (never the hub lock, never Qt)."""
        if name == self._label:
            self._store_position({self.axes[0]: x, self.axes[1]: y})

    def _store_position(self, positions: Dict[str, float]) -> None:
        with self._track_lock:
            self.updateTrackedPosition(positions)

    # ----------------------------------------------------------------- motion
    def _use(self, what: str):
        """``(host, label)`` of the connected device; ``DeviceNotConnectedError`` when it is not there."""
        self._requireConnected(what)
        binding = self._binding                       # the absent stand-in raises on any attribute
        host = binding.host
        if not host.is_connected:
            raise DeviceNotConnectedError(f"{self.name}: {what} refused, Micro-Manager core {self._hub.name!r} is not connected")
        return host, binding.label

    def _check_axis(self, axis: str) -> None:
        if axis not in self.axes:
            raise ValueError(f'Axis {axis} not available. Available axes: {self.axes}')

    def move(self, dist: float, axis: str):
        """Move ``axis`` by ``dist`` micrometres; returns the position (read back after the wait)."""
        return self._move(float(dist), axis, relative=True)

    def setPosition(self, position: float, axis: str):
        """Move ``axis`` to ``position`` micrometres; returns the position (read back after the wait)."""
        return self._move(float(position), axis, relative=False)

    def _move(self, value: float, axis: str, *, relative: bool) -> Dict[str, float]:
        self._check_axis(axis)
        host, label = self._use('move')
        with host.lock:
            core = host.core
            if not self._is_xy:
                if relative:
                    core.setRelativePosition(label, value)
                else:
                    core.setPosition(label, value)
            elif relative:
                core.setRelativeXYPosition(label, value if axis == self.axes[0] else 0.0,
                                           value if axis == self.axes[1] else 0.0)
            else:
                x, y = core.getXYPosition(label)          # the other axis stays where the device has it
                if axis == self.axes[0]:
                    x = value
                else:
                    y = value
                core.setXYPosition(label, x, y)
        if self._wait_for_device:
            self._wait_until_idle(host, label)
            with host.lock:
                self._store_position(self._read_position(host.core))
        with self._track_lock:
            return dict(self._position)

    def _wait_until_idle(self, host, label: str) -> None:
        """Block until the device reports not busy, polling OUTSIDE the hub lock.

        ``core.waitForDevice`` would hold the lock for the whole move and starve the
        camera drain thread. The deadline is MMCore's own device timeout
        (``getTimeoutMs``, the value ``waitForDevice`` enforces too), so a stuck stage
        fails the same way it would in Micro-Manager.
        """
        with host.lock:
            timeout_s = float(host.core.getTimeoutMs()) / 1000.0
        deadline = time.monotonic() + timeout_s
        while True:
            with host.lock:
                busy = host.core.deviceBusy(label)
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise MMCoreHostError(
                    f"positioner {self._setupName!r}: device {label!r} still busy after "
                    f"{timeout_s:.1f} s (MMCore device timeout, core.setTimeoutMs)")
            self._idle_poll.wait(BUSY_POLL_S)

    def updatePosition(self) -> None:
        """Re-read the position from the device (the positioner controller calls this hook before it
        displays a position). The authoritative read for ``waitForDevice: false``, and the only one
        for adapters that send no position events."""
        host, label = self._use('updatePosition')
        with host.lock:
            self._store_position(self._read_position(host.core))

    # -------------------------------------------------------------- reference
    def reference(self, axis: str = None, position=None) -> None:
        """Home the device (``homeable`` only), wait for it and mark it referenced.

        Micro-Manager homes the whole device, so ``axis`` only selects what is reported; the
        stage's own origin is what the home command defines, so ``position`` is refused. An
        adapter without a home command raises Micro-Manager's own error here (and the device
        stays unreferenced).
        """
        if not self.requiresReference:
            return super().reference(axis, position)
        if axis is not None:
            self._check_axis(axis)
        if position is not None:
            raise ValueError(
                f"positioner {self._setupName!r}: referencing a Micro-Manager stage homes it to the "
                f"adapter's own origin; a reference position ({position!r}) cannot be set")
        host, label = self._use('reference')
        with host.lock:
            host.core.home(label)
        self._wait_until_idle(host, label)
        with host.lock:
            self._store_position(self._read_position(host.core))
        self.markReferenced()

    # --------------------------------------------------------------- lifecycle
    def _lifecycleSafeState(self, *, verified: bool):
        return []                       # a stage does not move by itself

    def _onTransportReconnected(self, real: bool):
        """After the hub reloaded the core: adopt the device again; the errors found, if any."""
        if not real:
            details = self._hub.connectionStatusDetails or 'transport unavailable'
            self._setConnectionError(details, summary=f'{self._label}: Micro-Manager core did not reconnect')
            return [details]
        errors: List[str] = []
        try:
            self.backendHolder.reopen()
            if not self.backendIsReal:
                raise DeviceNotConnectedError(self.connectionStatusDetails or 'the hub is not connected')
            errors.extend(self._reinitialise())
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            errors.append(f'{type(exc).__name__}: {exc}')
        if errors:
            self._setConnectionError('; '.join(errors), summary=f'{self._label}: reconnected but not usable')
            return errors
        self._setConnected(f'{self._model}: {self._label} reconnected')
        return errors

    def _lifecycleReinitialise(self) -> None:
        errors = self._reinitialise()
        if errors:
            raise RuntimeError('; '.join(errors))

    def _reinitialise(self) -> List[str]:
        """Adopt the (re)loaded device: re-load an inline one, verify type and axes, re-read the
        position (the MHXY ``syncPositionFromHardware`` pattern) and follow its events again."""
        try:
            adopted = self._adopt()
            self._is_xy = adopted.is_xy
            self._model = adopted.model
            self._store_position(adopted.position)
            self._connect_events()
            if self.requiresReference:
                # A (re)loaded device: whatever `home` established is not known to
                # survive, so the stage must be referenced again before it counts as such.
                self.markUnreferenced()
            self._ready = True
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            return [f'{type(exc).__name__}: {exc}']
        return []

    def finalize(self):
        """Stop following events, unload an inline device, then the base class."""
        self._disconnect_events()
        self._unload_inline()
        super().finalize()
        self._setFinalizedStatus()

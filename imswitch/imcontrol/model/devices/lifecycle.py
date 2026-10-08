from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .graph import HardwareDeviceId
from .status import DeviceId


class DeviceLifecycleAction(str, Enum):
    """Runtime transitions understood by the lifecycle layer."""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PROBE = "probe"
    RECONNECT = "reconnect"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class DeviceLifecycleCapabilities:
    """Explicit opt-in capabilities for one physical-device lifecycle."""

    connect: bool = False
    disconnect: bool = False
    probe: bool = False
    reconnect: bool = False
    shutdown: bool = False

    def supports(self, action: DeviceLifecycleAction) -> bool:
        return bool(getattr(self, DeviceLifecycleAction(action).value))


@dataclass(frozen=True)
class DeviceLifecycleResult:
    """Result of one physical-device lifecycle transition.

    ``affected_device_ids`` identifies logical managers represented by the
    physical device. ``deactivated_device_ids`` is the narrower set whose
    hazardous/active state was deliberately reset by the transition; UI
    controllers can use it to synchronize their desired-state controls.
    """

    hardware_id: HardwareDeviceId
    action: DeviceLifecycleAction
    success: bool
    summary: str
    details: str | None = None
    affected_device_ids: tuple[DeviceId, ...] = ()
    deactivated_device_ids: tuple[DeviceId, ...] = ()


class DeviceLifecycleError(RuntimeError):
    """Base exception for lifecycle orchestration errors."""


class DeviceLifecycleNotSupportedError(DeviceLifecycleError):
    """Raised when a physical device did not opt in to a requested action."""


class DeviceLifecycleBusyError(DeviceLifecycleError):
    """Raised when the same physical device is already transitioning."""


class DeviceLifecycleBlockedError(DeviceLifecycleError):
    """Raised when application ownership makes a transition unsafe."""


@runtime_checkable
class DeviceLifecycle(Protocol):
    """Optional lifecycle contract for one user-meaningful physical device.

    Implementations are vendor/device specific. Unsupported methods may raise
    ``DeviceLifecycleNotSupportedError``; callers must check ``capabilities``
    before invoking them. Normal device commands do not flow through this
    object -- it only coordinates runtime connection transitions.
    """

    @property
    def hardware_id(self) -> HardwareDeviceId:
        ...

    @property
    def capabilities(self) -> DeviceLifecycleCapabilities:
        ...

    def connect(self) -> DeviceLifecycleResult:
        ...

    def disconnect(self) -> DeviceLifecycleResult:
        ...

    def probe(self) -> DeviceLifecycleResult:
        ...

    def reconnect(self) -> DeviceLifecycleResult:
        ...

    def shutdown(self) -> DeviceLifecycleResult:
        ...


@runtime_checkable
class DeviceLifecycleProvider(Protocol):
    """Manager-side opt-in hook used while building physical-device handles.

    The hook must only return/configure already-existing runtime objects. It
    must not contact hardware. Several logical managers belonging to the same
    physical device should return the same lifecycle instance.
    """

    def getDeviceLifecycle(self) -> DeviceLifecycle | None:
        ...


@dataclass(frozen=True)
class DeviceHandle:
    """Stable runtime handle for one physical device.

    A handle always exists for a graph-backed physical device. ``lifecycle``
    is ``None`` for legacy/default-deny devices.
    """

    hardware_id: HardwareDeviceId
    source_device_ids: tuple[DeviceId, ...]
    lifecycle: DeviceLifecycle | None = None

    @property
    def capabilities(self) -> DeviceLifecycleCapabilities:
        if self.lifecycle is None:
            return DeviceLifecycleCapabilities()
        return self.lifecycle.capabilities


class BackendLifecycle:
    """The lifecycle of a manager that opened its backend through a
    :class:`~.status.BackendHolder` -- the template every device gets by
    default (``docs/design/plans/device-reconnect-2.0.md`` §4.2):

    1. ``_lifecycleSafeState(verified=False)`` -- best effort, errors ignored;
    2. ``_replaceBackend()`` -- close, open the real backend again;
    3. ``_lifecycleReinitialise()`` -- settings, a fresh position read;
    4. ``_lifecycleSafeState(verified=True)`` -- any error fails the reconnect.

    A detector manager's replacement runs inside its
    ``DetectorsManager.detectorLifecycleMaintenance`` window (set through
    :meth:`bindDetectors`) and ends with the acquisition stopped.

    ``hardware_id`` is bound by the lifecycle service from the device graph,
    so a manager needs no descriptor declaration of its own.
    """

    def __init__(self, manager) -> None:
        self._manager = manager
        self.hardware_id = None
        self._detectors = None
        self._lock = threading.RLock()

    @property
    def capabilities(self) -> DeviceLifecycleCapabilities:
        holder = self._manager.backendHolder
        transient = bool(holder is not None and holder.transient)
        return DeviceLifecycleCapabilities(
            connect=transient, disconnect=transient, reconnect=True)

    def bindHardwareId(self, hardware_id) -> None:
        self.hardware_id = hardware_id

    def bindDetectors(self, detectorsManager) -> None:
        """For a detector: the manager whose maintenance window the
        replacement must run in."""
        self._detectors = detectorsManager

    # ------------------------------------------------------------ helpers
    def _ids(self):
        name = getattr(self._manager, "name", None)
        kind = self.hardware_id.category if self.hardware_id is not None else "device"
        return (DeviceId(kind, name),) if name is not None else ()

    def _result(self, action, success, summary, details=None, *, deactivated=()):
        return DeviceLifecycleResult(
            hardware_id=self.hardware_id, action=action, success=success,
            summary=summary, details=details, affected_device_ids=self._ids(),
            deactivated_device_ids=tuple(deactivated),
        )

    def _maintenance(self):
        name = getattr(self._manager, "name", None)
        if self._detectors is not None and name is not None:
            return self._detectors.detectorLifecycleMaintenance(name)
        return contextlib.nullcontext()

    def _replaceAndReinitialise(self, action):
        """Steps 1-4; returns ``(real, errors)``."""
        manager = self._manager
        label = manager.backendHolder.label
        try:
            manager._lifecycleSafeState(verified=False)
        except Exception:
            pass
        with self._maintenance():
            try:
                real = manager._replaceBackend()
            except Exception as exc:
                return False, [f"{label}: replacing the backend failed: {exc}"]
            if self._detectors is not None and getattr(manager, "name", None) is not None:
                # A completed replacement makes any stop failure from the
                # retired backend obsolete.
                self._detectors.clearFaultAfterHardwareReplacement(manager.name)
            if not real:
                details = manager.connectionStatusDetails
                return False, [f"{label} did not connect" + (f": {details}" if details else "")]
            errors = []
            try:
                manager._lifecycleReinitialise()
            except Exception as exc:
                errors.append(f"{label}: re-initialisation failed: {exc}")
            try:
                errors.extend(manager._lifecycleSafeState(verified=True) or [])
            except Exception as exc:
                errors.append(f"{label}: safe state failed: {exc}")
            if errors:
                manager._setConnectionError("; ".join(errors),
                                            summary=f"{label} reconnected but not usable")
            return True, errors

    # ------------------------------------------------------------ actions
    def reconnect(self):
        with self._lock:
            real, errors = self._replaceAndReinitialise(DeviceLifecycleAction.RECONNECT)
            label = self._manager.backendHolder.label
            if errors:
                return self._result(DeviceLifecycleAction.RECONNECT, False, errors[0],
                                    "; ".join(errors[1:]) or None,
                                    deactivated=self._deactivated())
            return self._result(DeviceLifecycleAction.RECONNECT, True,
                                f"{label} reconnected", deactivated=self._deactivated())

    def connect(self):
        if not self.capabilities.connect:
            raise DeviceLifecycleNotSupportedError("Connect is for transient devices.")
        with self._lock:
            manager = self._manager
            label = manager.backendHolder.label
            if manager.backendIsReal:
                return self._result(DeviceLifecycleAction.CONNECT, True, "Already connected")
            real, errors = self._replaceAndReinitialise(DeviceLifecycleAction.CONNECT)
            if errors:
                return self._result(DeviceLifecycleAction.CONNECT, False, errors[0],
                                    "; ".join(errors[1:]) or None)
            return self._result(DeviceLifecycleAction.CONNECT, True, f"{label} connected")

    def disconnect(self):
        if not self.capabilities.disconnect:
            raise DeviceLifecycleNotSupportedError("Disconnect is for transient devices.")
        with self._lock:
            manager = self._manager
            label = manager.backendHolder.label
            try:
                manager._lifecycleSafeState(verified=False)
            except Exception:
                pass
            with self._maintenance():
                try:
                    manager.backendHolder.disconnect()
                except Exception as exc:
                    manager._setConnectionError(exc, summary=f"{label} did not disconnect cleanly")
                    return self._result(DeviceLifecycleAction.DISCONNECT, False,
                                        f"{label} did not disconnect cleanly", str(exc))
            return self._result(DeviceLifecycleAction.DISCONNECT, True, f"{label} disconnected",
                                deactivated=self._deactivated())

    def _deactivated(self):
        # Lasers always come back dark: the Laser panel shows OFF.
        return tuple(d for d in self._ids() if d.kind == "laser")

    def probe(self):
        raise DeviceLifecycleNotSupportedError("Probe is not available for this device.")

    def shutdown(self):
        raise DeviceLifecycleNotSupportedError("Devices shut down with ImSwitch.")


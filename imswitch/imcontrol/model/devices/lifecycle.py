from __future__ import annotations

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

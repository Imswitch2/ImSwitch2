from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class DeviceConnectionState(str, Enum):
    """Passive runtime connection state known to ImSwitch."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"
    ERROR = "error"


class DeviceRuntimeMode(str, Enum):
    """Whether a manager currently represents real or hardware-free operation."""

    REAL = "real"
    MOCK = "mock"


class DeviceFailureKind(str, Enum):
    """Small, user-facing failure vocabulary for device diagnostics."""

    MISSING_DEPENDENCY = "missing_dependency"
    DEVICE_NOT_FOUND = "device_not_found"
    CONNECTION_ERROR = "connection_error"
    CONFIGURATION_ERROR = "configuration_error"
    INITIALIZATION_ERROR = "initialization_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, order=True)
class DeviceId:
    """Stable runtime identity for one configured hardware endpoint."""

    kind: str
    name: str


@dataclass(frozen=True)
class DeviceStatus:
    """Read-only status snapshot for one configured device."""

    device_id: DeviceId
    manager_name: str
    connection: DeviceConnectionState = DeviceConnectionState.UNKNOWN
    mode: DeviceRuntimeMode = DeviceRuntimeMode.REAL
    summary: str | None = None
    details: str | None = None
    failure_kind: DeviceFailureKind | None = None

    @property
    def name(self) -> str:
        return self.device_id.name

    @property
    def kind(self) -> str:
        return self.device_id.kind


@runtime_checkable
class DeviceStatusProvider(Protocol):
    """Optional passive status contract for modern device managers.

    Implementations must only report already-known runtime state. This method
    must not probe hardware or otherwise perform device I/O. Active probing is
    deliberately outside the V1 status contract.
    """

    def getDeviceStatus(self) -> DeviceStatus:
        ...

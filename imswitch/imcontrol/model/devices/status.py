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
    NOT_APPLICABLE = "not_applicable"


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


class DeviceManagerStatusMixin:
    """Canonical passive status contract for hardware device managers.

    The contract is deliberately cached-only: reading these properties must
    never contact hardware.  The mixin has no ``__init__`` so existing manager
    constructor ordering is unchanged; properties lazily default to UNKNOWN /
    REAL until a concrete manager records stronger evidence.
    """

    @property
    def connectionState(self) -> DeviceConnectionState:
        return getattr(
            self, "_deviceConnectionState", DeviceConnectionState.UNKNOWN
        )

    @property
    def runtimeMode(self) -> DeviceRuntimeMode:
        return getattr(self, "_deviceRuntimeMode", DeviceRuntimeMode.REAL)

    @property
    def connectionStatusSummary(self) -> str | None:
        return getattr(self, "_deviceStatusSummary", None)

    @property
    def connectionStatusDetails(self) -> str | None:
        return getattr(self, "_deviceStatusDetails", None)

    @property
    def connectionFailureKind(self) -> DeviceFailureKind | None:
        return getattr(self, "_deviceFailureKind", None)

    def _setConnectionState(
        self,
        state: DeviceConnectionState,
        *,
        summary: str | None = None,
        details: str | None = None,
        failure_kind: DeviceFailureKind | None = None,
    ) -> None:
        self._deviceConnectionState = DeviceConnectionState(state)
        self._deviceStatusSummary = summary
        self._deviceStatusDetails = details
        self._deviceFailureKind = failure_kind

    def _setConnected(self, summary: str | None = None) -> None:
        self._deviceRuntimeMode = DeviceRuntimeMode.REAL
        self._setConnectionState(
            DeviceConnectionState.CONNECTED, summary=summary
        )

    def _setDisconnected(self, summary: str | None = None) -> None:
        self._setConnectionState(
            DeviceConnectionState.DISCONNECTED, summary=summary
        )

    def _setMockActive(self, summary: str | None = None) -> None:
        self._deviceRuntimeMode = DeviceRuntimeMode.MOCK
        self._setConnectionState(
            DeviceConnectionState.NOT_APPLICABLE,
            summary=summary or "Mock backend configured",
        )

    def _setFinalizedStatus(self) -> None:
        """Record shutdown without turning an intentional mock into disconnected."""
        if self.runtimeMode is DeviceRuntimeMode.REAL:
            self._setDisconnected("Device finalized")

    def _setConnectionError(
        self,
        error: Exception | str,
        *,
        summary: str | None = None,
        failure_kind: DeviceFailureKind = DeviceFailureKind.CONNECTION_ERROR,
        mock_active: bool = False,
    ) -> None:
        if failure_kind is DeviceFailureKind.CONNECTION_ERROR and isinstance(
            error, ImportError
        ):
            failure_kind = DeviceFailureKind.MISSING_DEPENDENCY
        if mock_active:
            self._deviceRuntimeMode = DeviceRuntimeMode.MOCK
        self._setConnectionState(
            DeviceConnectionState.ERROR,
            summary=summary or "Hardware connection failed",
            details=str(error),
            failure_kind=failure_kind,
        )

    def _setDeviceNotFound(
        self, error: Exception | str, *, summary: str | None = None
    ) -> None:
        self._setConnectionError(
            error,
            summary=summary or "Hardware device not found",
            failure_kind=DeviceFailureKind.DEVICE_NOT_FOUND,
        )


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

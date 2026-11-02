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
    #: Declared but intentionally not connected (a transient device): no
    #: backend, no mock, no error.
    ABSENT = "absent"


class DeviceFailureKind(str, Enum):
    """Small, user-facing failure vocabulary for device diagnostics."""

    MISSING_DEPENDENCY = "missing_dependency"
    DEVICE_NOT_FOUND = "device_not_found"
    CONNECTION_ERROR = "connection_error"
    CONFIGURATION_ERROR = "configuration_error"
    INITIALIZATION_ERROR = "initialization_error"
    UNKNOWN = "unknown"


class DeviceNotConnectedError(RuntimeError):
    """A command for a real device that is not connected (absent at startup,
    disconnected, or faulted). Nothing was sent; reconnect the device."""


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
        # Published last, in one assignment, for readers on other threads.
        # Read from the instance dict: Qt managers may not have run
        # QObject.__init__ yet, and attribute lookup then raises.
        self._deviceStatusSnapshot = (
            self._deviceConnectionState,
            self.__dict__.get('_deviceRuntimeMode', DeviceRuntimeMode.REAL),
            summary, details, failure_kind,
        )

    @property
    def isUsable(self) -> bool:
        """Whether commands reach a backend: a configured mock always, a
        real device only while connected (UNKNOWN counts as usable: legacy
        managers that never record status)."""
        mode = self.runtimeMode
        if mode is DeviceRuntimeMode.MOCK:
            return True
        return self.connectionState not in (
            DeviceConnectionState.ERROR, DeviceConnectionState.DISCONNECTED)

    def _requireConnected(self, what: str = "command") -> None:
        """Refuse a command on a real device that is not connected."""
        if self.isUsable:
            return
        summary = self.connectionStatusSummary or "not connected"
        raise DeviceNotConnectedError(
            f"{getattr(self, 'name', type(self).__name__)}: {what} refused, {summary}")

    # ------------------------------------------------------- backend holder
    def _installBackend(self, open_real, make_mock=None, *, label=None, **rules):
        """Open this manager's backend through a :class:`BackendHolder` and
        return it. ``open_real()`` raises when the hardware is absent;
        ``make_mock()`` builds the simulation (used only for a configured
        mock or the ``use_mock_on_failure`` opt-in). Rules:
        ``configured_mock``, ``use_mock_on_failure``, ``transient``,
        ``connect_on_startup``, ``close(backend)``.

        A real device that is not there gets an absent stand-in: every use
        raises :class:`DeviceNotConnectedError`. The manager is then
        reconnectable through its default lifecycle (``_replaceBackend``),
        and may override ``_lifecycleSafeState`` / ``_lifecycleReinitialise``.
        """
        label = label or getattr(self, "name", type(self).__name__)
        holder = BackendHolder(self, label=label, open_real=open_real,
                               make_mock=make_mock, **rules)
        self.__dict__["_backendHolder"] = holder
        return holder.open()

    @property
    def backendHolder(self):
        return self.__dict__.get("_backendHolder")

    @property
    def backendIsReal(self) -> bool:
        """Whether the current backend is real hardware (never a latch)."""
        holder = self.backendHolder
        return bool(holder is not None and holder.real)

    def _replaceBackend(self) -> bool:
        """Close the current backend and open the real one again; the
        default reconnect. Returns whether real hardware is now connected."""
        holder = self.backendHolder
        if holder is None:
            raise RuntimeError(f"{type(self).__name__} has no backend holder")
        return holder.reopen()

    def _lifecycleSafeState(self, *, verified: bool):
        """Put the device in its safe state around a reconnect; return the
        errors found (only read when ``verified``)."""
        return []

    def _lifecycleReinitialise(self) -> None:
        """Re-apply what the device must know after its backend was
        replaced (settings, a fresh position read)."""

    def getDeviceLifecycle(self):
        """The default lifecycle of a manager with a backend holder:
        reconnect, plus connect / disconnect for a transient device. The
        service binds its hardware id from the device graph."""
        holder = self.backendHolder
        if holder is None:
            return None
        lifecycle = self.__dict__.get("_backendLifecycle")
        if lifecycle is None:
            from .lifecycle import BackendLifecycle
            lifecycle = BackendLifecycle(self)
            host = self.__dict__.get("_detectorLifecycleHost")
            if host is not None:
                lifecycle.bindDetectors(host)
            self.__dict__["_backendLifecycle"] = lifecycle
        return lifecycle

    def _bindDetectorLifecycleHost(self, detectorsManager, detectorName) -> None:
        """Called by ``DetectorsManager`` for every detector: a detector's
        backend replacement must run in its maintenance window."""
        self.__dict__["_detectorLifecycleHost"] = detectorsManager
        lifecycle = self.__dict__.get("_backendLifecycle")
        if lifecycle is not None:
            lifecycle.bindDetectors(detectorsManager)

    def statusSnapshot(self):
        """(connection, mode, summary, details, failure_kind), consistent."""
        snapshot = self.__dict__.get("_deviceStatusSnapshot")
        if snapshot is not None:
            return snapshot
        return (self.connectionState, self.runtimeMode, self.connectionStatusSummary,
                self.connectionStatusDetails, self.connectionFailureKind)

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

    def _setAbsent(self, summary: str | None = None) -> None:
        """A transient device that is not connected -- not an error."""
        self._deviceRuntimeMode = DeviceRuntimeMode.ABSENT
        self._setConnectionState(
            DeviceConnectionState.DISCONNECTED,
            summary=summary or "Not connected",
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


class _AbsentBackend:
    """Stands in for the backend of a real device that is not connected:
    every use raises :class:`DeviceNotConnectedError`, so a manager's call
    sites need no "is it there" checks. Closing it is a no-op."""

    def __init__(self, manager, label: str) -> None:
        self.__dict__["_manager"] = manager
        self.__dict__["_label"] = label

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in ("close", "dispose", "finalize", "disconnect", "stop"):
            return lambda *a, **k: None
        manager = self.__dict__["_manager"]
        summary = getattr(manager, "connectionStatusSummary", None) or "not connected"
        raise DeviceNotConnectedError(
            f"{self.__dict__['_label']}: {name} refused, {summary}")

    def __bool__(self):
        return False

    def __repr__(self):
        return f"<absent backend of {self.__dict__['_label']}>"


class backend_attribute:
    """Descriptor for the manager attribute that holds the backend
    (``_stage``, ``_camera``, ``_driver``): it always reads the holder's
    *current* backend, so a reconnect that replaced it needs no bookkeeping
    at the call sites. Before a holder exists it behaves like a plain
    attribute."""

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        holder = obj.__dict__.get("_backendHolder")
        if holder is not None:
            return holder.backend
        return obj.__dict__.get(self._name)

    def __set__(self, obj, value):
        holder = obj.__dict__.get("_backendHolder")
        if holder is not None:
            holder.backend = value
            holder.real = holder.real and value is not None
        else:
            obj.__dict__[self._name] = value


class BackendHolder:
    """What opens a manager's backend, and how to open it again.

    One per manager (``DeviceManagerStatusMixin._installBackend``). Holds
    the two openers and the close call, applies the mode rules of
    ``docs/design/plans/device-reconnect-2.0.md`` §4.0, and gives every
    manager that uses it a reconnect (and, for a transient device, connect
    and disconnect) without code of its own.
    """

    def __init__(self, manager, *, label, open_real, make_mock=None,
                 configured_mock=False, use_mock_on_failure=False,
                 transient=False, connect_on_startup=False, close=None) -> None:
        self.manager = manager
        self.label = label
        self.open_real = open_real
        self.make_mock = make_mock
        self.configured_mock = bool(configured_mock)
        self.use_mock_on_failure = bool(use_mock_on_failure)
        self.transient = bool(transient)
        self.connect_on_startup = bool(connect_on_startup)
        self._close = close
        self.backend = None
        self.real = False

    # ---------------------------------------------------------- opening
    def open(self):
        """The startup open (§4.0, first column)."""
        if self.configured_mock:
            self.backend, self.real = self._mock(), False
            self.manager._setMockActive(f"{self.label}: mock configured")
            return self.backend
        if self.transient and not self.connect_on_startup:
            self.backend, self.real = _AbsentBackend(self.manager, self.label), False
            self.manager._setAbsent(f"{self.label} not connected -- connect it in Hardware status")
            return self.backend
        return self._openReal(attempt="startup")

    def reopen(self) -> bool:
        """Close whatever is installed and open the real backend again.
        Returns whether real hardware is now connected."""
        self.close(suppress_errors=True)
        self._openReal(attempt="reconnect")
        return self.real

    def disconnect(self) -> None:
        self.close(suppress_errors=False)
        self.backend, self.real = _AbsentBackend(self.manager, self.label), False
        self.manager._setAbsent(f"{self.label} disconnected")

    def close(self, *, suppress_errors: bool) -> None:
        backend, self.backend = self.backend, None
        self.real = False
        if backend is None or isinstance(backend, _AbsentBackend):
            return
        try:
            if self._close is not None:
                self._close(backend)
            else:
                for name in ("close", "dispose", "finalize"):
                    method = getattr(backend, name, None)
                    if callable(method):
                        method()
                        break
        except Exception:
            if not suppress_errors:
                raise

    def _openReal(self, *, attempt: str):
        try:
            backend = self.open_real()
        except Exception as exc:
            if self.use_mock_on_failure and self.make_mock is not None:
                self.backend, self.real = self._mock(), False
                self.manager._setConnectionError(
                    exc, summary=f"{self.label} failed at {attempt}; mock fallback active",
                    mock_active=True)
            else:
                self.backend, self.real = _AbsentBackend(self.manager, self.label), False
                if self.manager.runtimeMode is DeviceRuntimeMode.MOCK:
                    # An opt-in mock from an earlier failure is gone now.
                    self.manager._deviceRuntimeMode = DeviceRuntimeMode.REAL
                self.manager._setConnectionError(
                    exc, summary=f"{self.label} not connected")
            return self.backend
        self.backend, self.real = backend, True
        self.manager._setConnected(f"{self.label} connected")
        return backend

    def _mock(self):
        if self.make_mock is None:
            raise RuntimeError(f"{self.label}: no mock available")
        return self.make_mock()


def device_usable(manager) -> bool:
    """Whether a panel may send commands to ``manager``: its ``isUsable``,
    or True for a manager that records no status."""
    return bool(getattr(manager, "isUsable", True))


def not_connected_reason(manager) -> str:
    """What to show on a greyed-out row."""
    summary = getattr(manager, "connectionStatusSummary", None)
    details = getattr(manager, "connectionStatusDetails", None)
    text = summary or "Not connected"
    if details and details not in text:
        text = f"{text}: {details}"
    return f"{text}\nReconnect it from Hardware \u2192 Hardware status."


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

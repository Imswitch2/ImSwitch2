from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.MultiManager import MultiManager

from .status import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
    DeviceStatus,
)


_MULTI_MANAGER_SOURCES = (
    ("detector", "detectorsManager"),
    ("laser", "lasersManager"),
    ("positioner", "positionersManager"),
    ("rotator", "rotatorsManager"),
    ("flip_mirror", "flipMirrorsManager"),
    ("rs232", "rs232sManager"),
    ("slm", "slmsManager"),
)

_SINGLE_MANAGER_SOURCES = (
    ("daq", "NI-DAQ", "nidaqManager"),
    ("pulse_generator", "Pulse generator", "pulseGeneratorManager"),
    ("trigger_scope", "TriggerScope", "triggerScopeManager"),
    ("stand", "Microscope stand", "standManager"),
)

# These are intentionally limited to passive values stored directly on the
# manager. Do not call arbitrary isConnected()/is_connected() methods here:
# in existing drivers those may perform hardware I/O.
_PASSIVE_CONNECTION_KEYS = (
    "connected",
    "_connected",
    "_is_connected",
    "_isConnected",
)

_PASSIVE_MOCK_KEYS = (
    "mockermode",
    "mocker",
    "_mock_fallback",
    "_simulation",
    "_simulation_mode",
)


def _manager_state(manager) -> dict:
    try:
        return object.__getattribute__(manager, "__dict__")
    except Exception:
        return {}


def _read_passive_bool(state: dict, keys: Iterable[str]) -> bool | None:
    for key in keys:
        value = state.get(key)
        if isinstance(value, bool):
            return value
    return None


def _read_mangled_bool(state: dict, suffix: str) -> bool | None:
    for key, value in state.items():
        if key.endswith(suffix) and isinstance(value, bool):
            return value
    return None


def _looks_mock_from_class(manager) -> bool:
    class_name = type(manager).__name__.lower()
    module_name = type(manager).__module__.lower()
    return class_name.startswith("mock") or ".mock" in module_name or "_mock" in module_name


def _infer_runtime_mode(manager) -> DeviceRuntimeMode:
    state = _manager_state(manager)
    explicit = _read_passive_bool(state, _PASSIVE_MOCK_KEYS)
    if explicit is True:
        return DeviceRuntimeMode.MOCK

    # NI-DAQ deliberately stores this as a private ``__simulating`` flag.
    simulating = _read_mangled_bool(state, "__simulating")
    if simulating is True:
        return DeviceRuntimeMode.MOCK

    if _looks_mock_from_class(manager):
        return DeviceRuntimeMode.MOCK

    # Several existing managers wrap their mock implementation rather than
    # exposing a dedicated flag (notably camera managers). Inspect only a
    # small set of already-held object references; never call into them.
    for wrapped_key in ("_driver", "_camera", "_laser", "_device"):
        wrapped = state.get(wrapped_key)
        if wrapped is not None and _looks_mock_from_class(wrapped):
            return DeviceRuntimeMode.MOCK

    return DeviceRuntimeMode.REAL


def _infer_connection(manager, mode: DeviceRuntimeMode) -> DeviceConnectionState:
    state = _manager_state(manager)
    if state.get("_mock_fallback") is True:
        return DeviceConnectionState.ERROR

    connected = _read_passive_bool(state, _PASSIVE_CONNECTION_KEYS)
    if connected is True:
        return DeviceConnectionState.CONNECTED
    if connected is False:
        return DeviceConnectionState.DISCONNECTED

    # Do not call properties or connection-check methods: the V1 contract is
    # passive and must never touch hardware merely to refresh the dashboard.
    if mode is DeviceRuntimeMode.MOCK:
        # A successfully constructed mock is usable, but "connected" has a
        # hardware meaning. Leave it unknown unless the manager explicitly
        # stores passive connection state.
        return DeviceConnectionState.UNKNOWN
    return DeviceConnectionState.UNKNOWN


def _infer_summary(manager, mode: DeviceRuntimeMode) -> tuple[str | None, str | None, DeviceFailureKind | None]:
    state = _manager_state(manager)

    if state.get("_mock_fallback") is True:
        return (
            "Using mock fallback after hardware connection failure",
            None,
            DeviceFailureKind.CONNECTION_ERROR,
        )

    if mode is DeviceRuntimeMode.MOCK:
        return "Hardware-free/mock operation", None, None

    return None, None, None


def resolveDeviceStatus(device_id: DeviceId, manager) -> DeviceStatus:
    """Build a passive status snapshot without performing device I/O.

    Managers may opt into the explicit ``getDeviceStatus`` contract. Existing
    managers are handled conservatively from state already stored on the object.
    Unknown is preferred over assuming successful construction means connected.
    """
    provider = getattr(type(manager), "getDeviceStatus", None)
    if callable(provider):
        try:
            status = provider(manager)
        except Exception as exc:
            return DeviceStatus(
                device_id=device_id,
                manager_name=type(manager).__name__,
                connection=DeviceConnectionState.ERROR,
                mode=_infer_runtime_mode(manager),
                summary="Manager status provider failed",
                details=str(exc),
                failure_kind=DeviceFailureKind.UNKNOWN,
            )
        if isinstance(status, DeviceStatus):
            # The supervisor owns identity; a provider cannot accidentally
            # report another device's id.
            return replace(status, device_id=device_id)

    mode = _infer_runtime_mode(manager)
    connection = _infer_connection(manager, mode)
    summary, details, failure_kind = _infer_summary(manager, mode)
    return DeviceStatus(
        device_id=device_id,
        manager_name=type(manager).__name__,
        connection=connection,
        mode=mode,
        summary=summary,
        details=details,
        failure_kind=failure_kind,
    )


class DeviceSupervisor:
    """Read-only inventory and status facade over already-created managers.

    V1 deliberately does not own manager construction, reconnection, probing,
    or failure recovery. It only observes managers that the existing startup
    path successfully created.
    """

    def __init__(self, master):
        self.__logger = initLogger(self)
        self._master = master

    def _iterDevices(self):
        for kind, attr_name in _MULTI_MANAGER_SOURCES:
            manager_group = getattr(self._master, attr_name, None)
            if manager_group is None or not isinstance(manager_group, MultiManager):
                continue
            for name, manager in manager_group:
                if manager is not None:
                    yield DeviceId(kind=kind, name=str(name)), manager

        for kind, display_name, attr_name in _SINGLE_MANAGER_SOURCES:
            manager = getattr(self._master, attr_name, None)
            if manager is None:
                continue

            # StandManager is a wrapper around the actual stand implementation.
            # Keep one user-facing device entry, but resolve status from the
            # wrapped implementation when present so passive connection state is
            # not hidden by the facade.
            status_manager = manager
            if attr_name == "standManager":
                wrapped = _manager_state(manager).get("_subManager")
                if wrapped is not None:
                    status_manager = wrapped

            yield DeviceId(kind=kind, name=display_name), status_manager

    def getAllStatuses(self) -> tuple[DeviceStatus, ...]:
        statuses = []
        for device_id, manager in self._iterDevices():
            try:
                statuses.append(resolveDeviceStatus(device_id, manager))
            except Exception as exc:
                self.__logger.warning(
                    "Could not resolve status for %s/%s: %s",
                    device_id.kind,
                    device_id.name,
                    exc,
                    exc_info=True,
                )
                statuses.append(
                    DeviceStatus(
                        device_id=device_id,
                        manager_name=type(manager).__name__,
                        connection=DeviceConnectionState.ERROR,
                        mode=_infer_runtime_mode(manager),
                        summary="Could not read passive device status",
                        details=str(exc),
                        failure_kind=DeviceFailureKind.UNKNOWN,
                    )
                )
        return tuple(sorted(statuses, key=lambda item: (item.kind, item.name.lower())))

    def getStatus(self, device_id: DeviceId) -> DeviceStatus:
        for status in self.getAllStatuses():
            if status.device_id == device_id:
                return status
        raise KeyError(f"Unknown device {device_id.kind}/{device_id.name}")

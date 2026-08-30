from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.MultiManager import MultiManager

from .graph import (
    DeviceDependencySpec,
    DeviceDescriptor,
    DeviceDescriptorSpec,
    DeviceGraph,
    DeviceRelation,
    DeviceRelationKind,
    DeviceRole,
    DeviceSection,
    HardwareDependencyStatus,
    HardwareDeviceId,
    HardwareStatus,
)
from .status import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceManagerStatusMixin,
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

_INFRASTRUCTURE_KINDS = {"daq", "pulse_generator", "trigger_scope", "rs232"}

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
    """Build a passive status snapshot without performing device I/O."""
    if isinstance(manager, DeviceManagerStatusMixin):
        return DeviceStatus(
            device_id=device_id,
            manager_name=type(manager).__name__,
            connection=manager.connectionState,
            mode=manager.runtimeMode,
            summary=manager.connectionStatusSummary,
            details=manager.connectionStatusDetails,
            failure_kind=manager.connectionFailureKind,
        )

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


def _default_descriptor_spec(device_id: DeviceId) -> DeviceDescriptorSpec:
    section = (
        DeviceSection.INFRASTRUCTURE
        if device_id.kind in _INFRASTRUCTURE_KINDS
        else DeviceSection.DEVICES
    )
    category = "infrastructure" if section is DeviceSection.INFRASTRUCTURE else device_id.kind
    return DeviceDescriptorSpec(category=category, section=section)


def resolveDeviceDescriptor(device_id: DeviceId, manager) -> tuple[DeviceDescriptor, tuple[DeviceRelation, ...]]:
    """Resolve config-only relationships for one already-created manager."""
    spec = _default_descriptor_spec(device_id)
    provider = getattr(type(manager), "getDeviceDescriptorSpec", None)
    if callable(provider):
        candidate = provider(manager)
        if isinstance(candidate, DeviceDescriptorSpec):
            spec = candidate

    category = spec.category or device_id.kind
    hardware_id = spec.hardware_id or HardwareDeviceId(
        category=category,
        key=f"{device_id.kind}:{device_id.name}",
    )
    descriptor = DeviceDescriptor(
        source_device_id=device_id,
        hardware_id=hardware_id,
        display_name=spec.display_name or device_id.name,
        category=category,
        role=spec.role,
        section=spec.section,
        manager_name=type(manager).__name__,
    )

    relations = []
    if descriptor.role is DeviceRole.COMPONENT:
        relations.append(
            DeviceRelation(
                source=device_id,
                kind=DeviceRelationKind.COMPONENT_OF,
                target=hardware_id,
            )
        )
    for dependency in spec.dependencies:
        relations.append(
            DeviceRelation(
                source=hardware_id,
                kind=dependency.kind,
                target=dependency.target,
                label=dependency.label,
            )
        )
    return descriptor, tuple(relations)


def _status_rank(status: DeviceStatus) -> int:
    # Higher means more important when several manager endpoints represent one
    # physical device. CONNECTED beats UNKNOWN because one successful protocol
    # exchange is enough to prove a shared controller exists; any known failure
    # still takes precedence over success.
    return {
        DeviceConnectionState.NOT_APPLICABLE: 0,
        DeviceConnectionState.UNKNOWN: 1,
        DeviceConnectionState.CONNECTED: 2,
        DeviceConnectionState.DISCONNECTED: 3,
        DeviceConnectionState.ERROR: 4,
    }[status.connection]


class DeviceSupervisor:
    """Read-only device graph + status facade over already-created managers.

    The supervisor normalizes the manager-oriented runtime into user-facing
    physical devices and infrastructure. Refresh remains cached-only: graph
    building and status aggregation never probe hardware.
    """

    def __init__(self, master, setupInfo=None):
        self.__logger = initLogger(self)
        self._master = master
        self._setupInfo = setupInfo

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

            status_manager = manager
            if attr_name == "standManager":
                wrapped = _manager_state(manager).get("_subManager")
                if wrapped is not None:
                    status_manager = wrapped

            yield DeviceId(kind=kind, name=display_name), status_manager

    def _rawEntries(self):
        return tuple(self._iterDevices())

    def getAllStatuses(self) -> tuple[DeviceStatus, ...]:
        """Raw manager-endpoint statuses retained for diagnostics/back-compat."""
        statuses = []
        for device_id, manager in self._rawEntries():
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

    def _configuredControlBackendRelations(self, descriptor: DeviceDescriptor):
        setup = self._setupInfo
        if setup is None or descriptor.source_device_id.kind not in {
            "detector", "laser", "positioner"
        }:
            return ()

        section_name = {
            "detector": "detectors",
            "laser": "lasers",
            "positioner": "positioners",
        }[descriptor.source_device_id.kind]
        infos = getattr(setup, section_name, None) or {}
        info = infos.get(descriptor.source_device_id.name)
        if info is None:
            return ()

        targets = set()
        for channel in (info.getAnalogChannel(), info.getDigitalLine()):
            if channel is None:
                continue
            text = str(channel).strip()
            if "/" not in text:
                continue
            backend_name = text.split("/", 1)[0].strip()
            if not backend_name:
                continue
            if backend_name.casefold() == "triggerscope":
                target = HardwareDeviceId("infrastructure", "triggerscope")
                label = "TriggerScope"
            else:
                target = HardwareDeviceId("infrastructure", f"nidaq:{backend_name}")
                label = f"NI-DAQ {backend_name}"
            targets.add((target, label))

        return tuple(
            DeviceRelation(
                source=descriptor.hardware_id,
                kind=DeviceRelationKind.USES_CONTROL_BACKEND,
                target=target,
                label=label,
            )
            for target, label in sorted(targets, key=lambda item: item[1].casefold())
        )

    def _nidaqDescriptorsAndStatuses(self):
        manager = getattr(self._master, "nidaqManager", None)
        if manager is None:
            return (), {}

        configured = set(getattr(manager, "configuredDeviceNames", ()) or ())
        detected = set(getattr(manager, "detectedDeviceNames", ()) or ())
        error = getattr(manager, "deviceEnumerationError", None)
        simulating = bool(getattr(manager, "simulating", False))

        names = sorted(configured | detected, key=str.casefold)
        descriptors = []
        statuses = {}
        for name in names:
            source_id = DeviceId("nidaq_board", str(name))
            hardware_id = HardwareDeviceId("infrastructure", f"nidaq:{name}")
            descriptors.append(
                DeviceDescriptor(
                    source_device_id=source_id,
                    hardware_id=hardware_id,
                    display_name=f"NI-DAQ {name}",
                    category="infrastructure",
                    role=DeviceRole.PRIMARY,
                    section=DeviceSection.INFRASTRUCTURE,
                    manager_name=type(manager).__name__,
                )
            )

            if simulating:
                status = DeviceStatus(
                    source_id,
                    type(manager).__name__,
                    connection=DeviceConnectionState.NOT_APPLICABLE,
                    mode=DeviceRuntimeMode.MOCK,
                    summary="NI-DAQ simulation configured",
                )
            elif error is not None:
                status = DeviceStatus(
                    source_id,
                    type(manager).__name__,
                    connection=DeviceConnectionState.ERROR,
                    mode=DeviceRuntimeMode.REAL,
                    summary="NI-DAQmx device enumeration failed",
                    details=str(error),
                    failure_kind=DeviceFailureKind.CONNECTION_ERROR,
                )
            elif name in detected:
                status = DeviceStatus(
                    source_id,
                    type(manager).__name__,
                    connection=DeviceConnectionState.CONNECTED,
                    mode=DeviceRuntimeMode.REAL,
                    summary="Detected by NI-DAQmx",
                )
            else:
                status = DeviceStatus(
                    source_id,
                    type(manager).__name__,
                    connection=DeviceConnectionState.ERROR,
                    mode=DeviceRuntimeMode.REAL,
                    summary=f'Configured NI-DAQ board "{name}" not found',
                    failure_kind=DeviceFailureKind.DEVICE_NOT_FOUND,
                )
            statuses[source_id] = status

        return tuple(descriptors), statuses

    def getDeviceGraph(self) -> DeviceGraph:
        descriptors = []
        relations = []
        seen_relations = set()

        for device_id, manager in self._rawEntries():
            # The old singleton NI-DAQ manager is replaced in the user graph by
            # one infrastructure node per discovered/configured board.
            if device_id.kind == "daq":
                continue
            try:
                descriptor, manager_relations = resolveDeviceDescriptor(device_id, manager)
            except Exception as exc:
                self.__logger.warning(
                    "Could not resolve device descriptor for %s/%s: %s",
                    device_id.kind,
                    device_id.name,
                    exc,
                    exc_info=True,
                )
                descriptor, manager_relations = resolveDeviceDescriptor(device_id, object())

            descriptors.append(descriptor)
            for relation in (*manager_relations, *self._configuredControlBackendRelations(descriptor)):
                key = (relation.source, relation.kind, relation.target, relation.label)
                if key not in seen_relations:
                    seen_relations.add(key)
                    relations.append(relation)

        nidaq_descriptors, _ = self._nidaqDescriptorsAndStatuses()
        descriptors.extend(nidaq_descriptors)

        return DeviceGraph(tuple(descriptors), tuple(relations))

    def _aggregateHardwareStatus(self, descriptors, source_statuses) -> HardwareStatus:
        relevant = [
            source_statuses[d.source_device_id]
            for d in descriptors
            if d.source_device_id in source_statuses and d.role is not DeviceRole.RESOURCE
        ]
        if not relevant:
            raise ValueError("Cannot aggregate hardware without a status-bearing endpoint")

        selected = max(relevant, key=_status_rank)
        all_mock = all(status.mode is DeviceRuntimeMode.MOCK for status in relevant)
        mode = DeviceRuntimeMode.MOCK if all_mock else selected.mode

        primary = next((d for d in descriptors if d.role is DeviceRole.PRIMARY), descriptors[0])
        components = tuple(
            sorted(
                (d.source_device_id for d in descriptors if d.role is DeviceRole.COMPONENT),
                key=lambda item: (item.kind, item.name.casefold()),
            )
        )
        managers = tuple(sorted({d.manager_name for d in descriptors}))
        return HardwareStatus(
            hardware_id=primary.hardware_id,
            name=primary.display_name,
            category=primary.category,
            section=primary.section,
            connection=selected.connection,
            mode=mode,
            summary=selected.summary,
            details=selected.details,
            failure_kind=selected.failure_kind,
            manager_names=managers,
            components=components,
        )

    @staticmethod
    def _hardwareAsDeviceStatus(status: HardwareStatus) -> DeviceStatus:
        return DeviceStatus(
            device_id=DeviceId("hardware", status.name),
            manager_name=", ".join(status.manager_names) or "DeviceSupervisor",
            connection=status.connection,
            mode=status.mode,
            summary=status.summary,
            details=status.details,
            failure_kind=status.failure_kind,
        )

    def getHardwareStatuses(self) -> tuple[HardwareStatus, ...]:
        graph = self.getDeviceGraph()
        raw_statuses = {status.device_id: status for status in self.getAllStatuses()}
        _, nidaq_statuses = self._nidaqDescriptorsAndStatuses()
        source_statuses = {**raw_statuses, **nidaq_statuses}

        grouped = defaultdict(list)
        for descriptor in graph.descriptors:
            grouped[descriptor.hardware_id].append(descriptor)

        hardware = {}
        for hardware_id, descriptors in grouped.items():
            # Pure transport/resource nodes are retained in the graph for
            # diagnostics but intentionally omitted from the top-level UI.
            if all(d.role is DeviceRole.RESOURCE for d in descriptors):
                continue
            try:
                hardware[hardware_id] = self._aggregateHardwareStatus(
                    descriptors, source_statuses
                )
            except ValueError:
                continue

        raw_target_statuses = dict(source_statuses)
        hardware_target_statuses = {
            hardware_id: self._hardwareAsDeviceStatus(status)
            for hardware_id, status in hardware.items()
        }

        resolved = {}
        for hardware_id, base in hardware.items():
            dependency_statuses = []
            transport_failure = None
            for relation in graph.relationsFrom(hardware_id):
                target_status = None
                if isinstance(relation.target, DeviceId):
                    target_status = raw_target_statuses.get(relation.target)
                elif isinstance(relation.target, HardwareDeviceId):
                    target_status = hardware_target_statuses.get(relation.target)
                if target_status is None:
                    continue
                dependency_statuses.append(
                    HardwareDependencyStatus(
                        kind=relation.kind,
                        name=relation.label or target_status.name,
                        status=target_status,
                    )
                )
                if (
                    relation.kind is DeviceRelationKind.USES_TRANSPORT
                    and target_status.connection
                    in {DeviceConnectionState.ERROR, DeviceConnectionState.DISCONNECTED}
                ):
                    transport_failure = (relation, target_status)

            current = replace(base, dependencies=tuple(dependency_statuses))
            if transport_failure is not None:
                relation, dependency = transport_failure
                label = relation.label or dependency.name
                connection = (
                    DeviceConnectionState.ERROR
                    if dependency.connection is DeviceConnectionState.ERROR
                    else DeviceConnectionState.DISCONNECTED
                )
                current = replace(
                    current,
                    connection=connection,
                    mode=(
                        DeviceRuntimeMode.MOCK
                        if dependency.mode is DeviceRuntimeMode.MOCK
                        else current.mode
                    ),
                    summary=f"Transport {label}: {dependency.summary or dependency.connection.value}",
                    details=dependency.details,
                    failure_kind=dependency.failure_kind or DeviceFailureKind.CONNECTION_ERROR,
                )
            resolved[hardware_id] = current

        return tuple(
            sorted(
                resolved.values(),
                key=lambda status: (
                    0 if status.section is DeviceSection.DEVICES else 1,
                    status.category,
                    status.name.casefold(),
                ),
            )
        )

    def getPhysicalDeviceStatuses(self) -> tuple[HardwareStatus, ...]:
        return tuple(
            status
            for status in self.getHardwareStatuses()
            if status.section is DeviceSection.DEVICES
        )

    def getInfrastructureStatuses(self) -> tuple[HardwareStatus, ...]:
        return tuple(
            status
            for status in self.getHardwareStatuses()
            if status.section is DeviceSection.INFRASTRUCTURE
        )

    def getEffectiveStatus(self, device_id: DeviceId) -> HardwareStatus:
        """Resolve a configured logical device to its physical-device status."""
        graph = self.getDeviceGraph()
        descriptor = next(
            (d for d in graph.descriptors if d.source_device_id == device_id),
            None,
        )
        if descriptor is None:
            raise KeyError(f"Unknown device {device_id.kind}/{device_id.name}")
        for status in self.getHardwareStatuses():
            if status.hardware_id == descriptor.hardware_id:
                return status
        raise KeyError(f"No physical-device status for {device_id.kind}/{device_id.name}")

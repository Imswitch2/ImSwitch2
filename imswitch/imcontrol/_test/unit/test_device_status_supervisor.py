from types import SimpleNamespace

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceManagerStatusMixin,
    DeviceRuntimeMode,
    DeviceStatus,
    DeviceSupervisor,
    resolveDeviceStatus,
)
from imswitch.imcontrol.model.managers.MultiManager import MultiManager


class _Group(MultiManager):
    def __init__(self, entries):
        self._subManagers = dict(entries)
        self._shutdownFinalizedSubManagerObjects = []


class _ConnectedReal:
    def __init__(self):
        self.connected = True


class _DisconnectedReal:
    def __init__(self):
        self._is_connected = False


class _Unknown:
    pass


class _Mock:
    def __init__(self):
        self.mockermode = True


class _FallbackMock:
    def __init__(self):
        self._mock_fallback = True


class _Simulated:
    def __init__(self):
        self._simulation = True


class _ActiveConnectionMethod:
    def __init__(self):
        self.called = False

    def is_connected(self):
        self.called = True
        raise AssertionError("status refresh must not probe hardware")


class _CanonicalStatus(DeviceManagerStatusMixin):
    # Deliberately conflicts with the legacy resolver conventions. Canonical
    # managers must not be reverse-engineered by DeviceSupervisor.
    connected = True
    mockermode = True


class _ExplicitProvider:
    def getDeviceStatus(self):
        return DeviceStatus(
            device_id=DeviceId("wrong", "wrong"),
            manager_name=type(self).__name__,
            connection=DeviceConnectionState.CONNECTED,
            mode=DeviceRuntimeMode.REAL,
            summary="explicit",
        )


class _StandWrapper:
    def __init__(self, submanager):
        self._subManager = submanager


def test_resolver_prefers_unknown_over_constructor_success():
    status = resolveDeviceStatus(DeviceId("laser", "L"), _Unknown())
    assert status.connection is DeviceConnectionState.UNKNOWN
    assert status.mode is DeviceRuntimeMode.REAL


def test_resolver_reads_only_passive_connection_state():
    assert resolveDeviceStatus(
        DeviceId("detector", "connected"), _ConnectedReal()
    ).connection is DeviceConnectionState.CONNECTED
    assert resolveDeviceStatus(
        DeviceId("detector", "disconnected"), _DisconnectedReal()
    ).connection is DeviceConnectionState.DISCONNECTED

    active = _ActiveConnectionMethod()
    status = resolveDeviceStatus(DeviceId("detector", "active"), active)
    assert status.connection is DeviceConnectionState.UNKNOWN
    assert active.called is False


def test_mock_and_simulation_collapse_to_mock_mode():
    for manager in (_Mock(), _FallbackMock(), _Simulated()):
        status = resolveDeviceStatus(DeviceId("laser", "L"), manager)
        assert status.mode is DeviceRuntimeMode.MOCK


def test_mock_fallback_keeps_failure_diagnostic():
    status = resolveDeviceStatus(DeviceId("laser", "L"), _FallbackMock())
    assert status.mode is DeviceRuntimeMode.MOCK
    assert status.connection is DeviceConnectionState.ERROR
    assert status.failure_kind is DeviceFailureKind.CONNECTION_ERROR
    assert "fallback" in status.summary.lower()


def test_canonical_status_contract_is_lazy_and_takes_precedence_over_inference():
    manager = _CanonicalStatus()
    device_id = DeviceId("laser", "canonical")

    status = resolveDeviceStatus(device_id, manager)
    assert status.connection is DeviceConnectionState.UNKNOWN
    assert status.mode is DeviceRuntimeMode.REAL

    manager._setConnected("hardware opened")
    status = resolveDeviceStatus(device_id, manager)
    assert status.connection is DeviceConnectionState.CONNECTED
    assert status.mode is DeviceRuntimeMode.REAL
    assert status.summary == "hardware opened"


def test_canonical_mock_semantics_use_not_applicable_or_error():
    manager = _CanonicalStatus()
    device_id = DeviceId("positioner", "mock")

    manager._setMockActive("mock configured")
    status = resolveDeviceStatus(device_id, manager)
    assert status.connection is DeviceConnectionState.NOT_APPLICABLE
    assert status.mode is DeviceRuntimeMode.MOCK

    manager._setConnectionError(
        RuntimeError("port unavailable"),
        summary="fallback mock active",
        mock_active=True,
    )
    status = resolveDeviceStatus(device_id, manager)
    assert status.connection is DeviceConnectionState.ERROR
    assert status.mode is DeviceRuntimeMode.MOCK
    assert status.failure_kind is DeviceFailureKind.CONNECTION_ERROR


def test_main_device_family_bases_expose_canonical_status_contract():
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import DetectorManager
    from imswitch.imcontrol.model.managers.lasers.LaserManager import LaserManager
    from imswitch.imcontrol.model.managers.positioners.PositionerManager import PositionerManager

    assert issubclass(DetectorManager, DeviceManagerStatusMixin)
    assert issubclass(LaserManager, DeviceManagerStatusMixin)
    assert issubclass(PositionerManager, DeviceManagerStatusMixin)

def test_explicit_provider_cannot_override_supervisor_identity():
    device_id = DeviceId("laser", "L")
    status = resolveDeviceStatus(device_id, _ExplicitProvider())
    assert status.device_id == device_id
    assert status.connection is DeviceConnectionState.CONNECTED
    assert status.summary == "explicit"


def test_supervisor_enumerates_multimanagers_and_singletons_without_services():
    detector = _ConnectedReal()
    stand = _DisconnectedReal()
    master = SimpleNamespace(
        detectorsManager=_Group({"Camera": detector}),
        lasersManager=_Group({}),
        positionersManager=_Group({}),
        rotatorsManager=_Group({}),
        flipMirrorsManager=_Group({}),
        rs232sManager=_Group({"COM": _Unknown()}),
        slmsManager=_Group({}),
        nidaqManager=_Simulated(),
        pulseGeneratorManager=None,
        triggerScopeManager=None,
        standManager=_StandWrapper(stand),
        recordingManager=object(),
        scanManager=object(),
    )

    statuses = DeviceSupervisor(master).getAllStatuses()
    by_id = {status.device_id: status for status in statuses}

    assert DeviceId("detector", "Camera") in by_id
    assert DeviceId("rs232", "COM") in by_id
    assert DeviceId("daq", "NI-DAQ") in by_id
    assert DeviceId("stand", "Microscope stand") in by_id
    assert all(status.kind not in {"recording", "scan"} for status in statuses)
    assert by_id[DeviceId("daq", "NI-DAQ")].mode is DeviceRuntimeMode.MOCK
    assert by_id[DeviceId("stand", "Microscope stand")].connection is DeviceConnectionState.DISCONNECTED


def test_supervisor_status_order_is_stable():
    master = SimpleNamespace(
        detectorsManager=_Group({"Z": _Unknown(), "A": _Unknown()}),
        lasersManager=_Group({"B": _Unknown()}),
        positionersManager=_Group({}),
        rotatorsManager=_Group({}),
        flipMirrorsManager=_Group({}),
        rs232sManager=_Group({}),
        slmsManager=_Group({}),
        nidaqManager=None,
        pulseGeneratorManager=None,
        triggerScopeManager=None,
        standManager=None,
    )
    statuses = DeviceSupervisor(master).getAllStatuses()
    assert [(s.kind, s.name) for s in statuses] == [
        ("detector", "A"),
        ("detector", "Z"),
        ("laser", "B"),
    ]


def _empty_master(**overrides):
    values = dict(
        detectorsManager=_Group({}),
        lasersManager=_Group({}),
        positionersManager=_Group({}),
        rotatorsManager=_Group({}),
        flipMirrorsManager=_Group({}),
        rs232sManager=_Group({}),
        slmsManager=_Group({}),
        nidaqManager=None,
        pulseGeneratorManager=None,
        triggerScopeManager=None,
        standManager=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _GraphTransport(DeviceManagerStatusMixin):
    def getDeviceDescriptorSpec(self):
        from imswitch.imcontrol.model.devices import DeviceDescriptorSpec, DeviceRole
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)


class _GraphCoolLedChannel(DeviceManagerStatusMixin):
    def __init__(self, rs232_name="coolLED"):
        self._rs232_name = rs232_name

    def getDeviceDescriptorSpec(self):
        from imswitch.imcontrol.model.devices import sharedRs232ComponentSpec
        return sharedRs232ComponentSpec(
            category="laser",
            family="coolled",
            display_name="CoolLED controller",
            rs232_name=self._rs232_name,
        )


class _FakeNidaq:
    def __init__(self, *, configured=(), detected=(), error=None, simulating=False):
        self.configuredDeviceNames = tuple(configured)
        self.detectedDeviceNames = tuple(detected)
        self.deviceEnumerationError = error
        self.simulating = simulating


class _SetupDeviceInfo:
    def __init__(self, *, analog=None, digital=None):
        self._analog = analog
        self._digital = digital

    def getAnalogChannel(self):
        return self._analog

    def getDigitalLine(self):
        return self._digital


class _Setup:
    def __init__(self, *, lasers=None, detectors=None, positioners=None):
        self.lasers = lasers or {}
        self.detectors = detectors or {}
        self.positioners = positioners or {}


def test_device_graph_collapses_shared_channels_and_keeps_transport_as_resource():
    from imswitch.imcontrol.model.devices import (
        DeviceRelationKind,
        DeviceRuntimeMode,
        DeviceConnectionState,
    )

    transport = _GraphTransport()
    transport._setConnectionError(
        RuntimeError("COM10 unavailable"),
        summary="RS232 transport COM10 failed; mock fallback active",
        mock_active=True,
    )
    channel_a = _GraphCoolLedChannel()
    channel_b = _GraphCoolLedChannel()

    master = _empty_master(
        lasersManager=_Group({"365 LED": channel_a, "435 LED": channel_b}),
        rs232sManager=_Group({"coolLED": transport}),
    )
    supervisor = DeviceSupervisor(master)

    graph = supervisor.getDeviceGraph()
    component_relations = [
        relation
        for relation in graph.relations
        if relation.kind is DeviceRelationKind.COMPONENT_OF
    ]
    assert len(component_relations) == 2

    statuses = supervisor.getHardwareStatuses()
    assert len(statuses) == 1  # RS232 resource is diagnostic-only, not a top-level row.
    coolled = statuses[0]
    assert coolled.name == "CoolLED controller"
    assert {component.name for component in coolled.components} == {"365 LED", "435 LED"}
    assert coolled.connection is DeviceConnectionState.ERROR
    assert coolled.mode is DeviceRuntimeMode.MOCK
    assert len(coolled.dependencies) == 1
    assert coolled.dependencies[0].kind is DeviceRelationKind.USES_TRANSPORT

    assert supervisor.getEffectiveStatus(DeviceId("laser", "365 LED")).hardware_id == coolled.hardware_id


def test_failed_nidaq_control_backend_does_not_become_laser_connection_failure():
    from imswitch.imcontrol.model.devices import (
        DeviceRelationKind,
        DeviceConnectionState,
        DeviceSection,
    )

    laser = _CanonicalStatus()  # canonical default is UNKNOWN / REAL
    nidaq = _FakeNidaq(configured=("Dev1",), detected=())
    setup = _Setup(
        lasers={"Analog laser": _SetupDeviceInfo(analog="Dev1/ao0")}
    )
    master = _empty_master(
        lasersManager=_Group({"Analog laser": laser}),
        nidaqManager=nidaq,
    )
    supervisor = DeviceSupervisor(master, setupInfo=setup)

    statuses = supervisor.getHardwareStatuses()
    laser_status = next(status for status in statuses if status.name == "Analog laser")
    daq_status = next(status for status in statuses if status.name == "NI-DAQ Dev1")

    assert laser_status.connection is DeviceConnectionState.UNKNOWN
    assert daq_status.connection is DeviceConnectionState.ERROR
    assert daq_status.section is DeviceSection.INFRASTRUCTURE
    assert len(laser_status.dependencies) == 1
    assert laser_status.dependencies[0].kind is DeviceRelationKind.USES_CONTROL_BACKEND
    assert laser_status.dependencies[0].status.connection is DeviceConnectionState.ERROR


def test_detected_nidaq_board_is_reported_as_connected_infrastructure():
    from imswitch.imcontrol.model.devices import (
        DeviceConnectionState,
        DeviceSection,
    )

    nidaq = _FakeNidaq(detected=("Dev2",))
    supervisor = DeviceSupervisor(_empty_master(nidaqManager=nidaq))
    statuses = supervisor.getInfrastructureStatuses()

    assert len(statuses) == 1
    assert statuses[0].name == "NI-DAQ Dev2"
    assert statuses[0].connection is DeviceConnectionState.CONNECTED
    assert statuses[0].section is DeviceSection.INFRASTRUCTURE


def test_connected_transport_alone_does_not_prove_shared_device_connected():
    transport = _GraphTransport()
    transport._setConnected("COM10 opened")
    channel = _GraphCoolLedChannel()
    supervisor = DeviceSupervisor(
        _empty_master(
            lasersManager=_Group({"365 LED": channel}),
            rs232sManager=_Group({"coolLED": transport}),
        )
    )

    coolled = supervisor.getPhysicalDeviceStatuses()[0]
    assert coolled.connection is DeviceConnectionState.UNKNOWN
    assert coolled.mode is DeviceRuntimeMode.REAL

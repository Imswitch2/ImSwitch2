from types import SimpleNamespace

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
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

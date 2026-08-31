from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceDependencySpec,
    DeviceDescriptorSpec,
    DeviceHandle,
    DeviceId,
    DeviceLifecycleAction,
    DeviceLifecycleBlockedError,
    DeviceLifecycleCapabilities,
    DeviceLifecycleResult,
    DeviceLifecycleService,
    DeviceManagerStatusMixin,
    DeviceRelationKind,
    DeviceRole,
    DeviceRuntimeMode,
    DeviceSupervisor,
    HardwareDeviceId,
)
from imswitch.imcontrol.model.managers.MultiManager import MultiManager


class _Group(MultiManager):
    def __init__(self, entries):
        self._subManagers = dict(entries)
        self._shutdownFinalizedSubManagerObjects = []


class _Lifecycle:
    def __init__(self, hardware_id):
        self.hardware_id = hardware_id
        self.capabilities = DeviceLifecycleCapabilities(reconnect=True)
        self.calls = 0

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def probe(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError

    def reconnect(self):
        self.calls += 1
        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=True,
            summary="reconnected",
        )


class _LifecycleManager(DeviceManagerStatusMixin):
    def __init__(self, lifecycle=None, *, name="device"):
        self.lifecycle = lifecycle
        self.name = name
        self._setConnected("connected")

    def getDeviceDescriptorSpec(self):
        hardware_id = (
            self.lifecycle.hardware_id
            if self.lifecycle is not None
            else HardwareDeviceId("laser", f"plain:{self.name}")
        )
        return DeviceDescriptorSpec(
            role=DeviceRole.PRIMARY,
            hardware_id=hardware_id,
            category="laser",
            display_name=self.name,
        )

    def getDeviceLifecycle(self):
        return self.lifecycle


def _master(*, lasers=None, active_run=None, recording=False):
    return SimpleNamespace(
        detectorsManager=_Group({}),
        lasersManager=_Group(lasers or {}),
        positionersManager=_Group({}),
        rotatorsManager=_Group({}),
        flipMirrorsManager=_Group({}),
        rs232sManager=_Group({}),
        slmsManager=_Group({}),
        nidaqManager=None,
        pulseGeneratorManager=None,
        triggerScopeManager=None,
        standManager=None,
        scanExecutionCoordinator=SimpleNamespace(
            activeRunToken=active_run,
            activeToken=None,
        ),
        recordingManager=SimpleNamespace(record=recording),
    )


def test_lifecycle_service_is_default_deny_and_builds_stable_physical_handles():
    hardware_id = HardwareDeviceId("laser", "managed:one")
    lifecycle = _Lifecycle(hardware_id)
    managed = _LifecycleManager(lifecycle, name="managed")
    legacy = _LifecycleManager(None, name="legacy")
    master = _master(lasers={"managed": managed, "legacy": legacy})
    supervisor = DeviceSupervisor(master)

    service = DeviceLifecycleService(master, supervisor)

    assert service.canReconnect(hardware_id) is True
    assert service.canReconnect(HardwareDeviceId("laser", "plain:legacy")) is False
    handle = service.getHandle(hardware_id)
    assert isinstance(handle, DeviceHandle)
    assert handle.lifecycle is lifecycle
    assert handle.source_device_ids == (DeviceId("laser", "managed"),)


def test_lifecycle_service_publishes_results_and_serializes_safety_guards():
    hardware_id = HardwareDeviceId("laser", "managed:one")
    lifecycle = _Lifecycle(hardware_id)
    manager = _LifecycleManager(lifecycle, name="managed")
    master = _master(lasers={"managed": manager})
    service = DeviceLifecycleService(master, DeviceSupervisor(master))
    published = []
    service.addListener(published.append)

    result = service.reconnect(hardware_id)

    assert result.success is True
    assert result.affected_device_ids == (DeviceId("laser", "managed"),)
    assert published == [result]
    assert lifecycle.calls == 1

    master.scanExecutionCoordinator.activeRunToken = object()
    with pytest.raises(DeviceLifecycleBlockedError, match="scan"):
        service.reconnect(hardware_id)
    assert lifecycle.calls == 1

    master.scanExecutionCoordinator.activeRunToken = None
    master.recordingManager.record = True
    with pytest.raises(DeviceLifecycleBlockedError, match="recording"):
        service.reconnect(hardware_id)
    assert lifecycle.calls == 1


class _SharedTransportManager(_LifecycleManager):
    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(
            role=DeviceRole.PRIMARY,
            hardware_id=self.lifecycle.hardware_id,
            category="laser",
            display_name=self.name,
            dependencies=(
                DeviceDependencySpec(
                    kind=DeviceRelationKind.USES_TRANSPORT,
                    target=DeviceId("rs232", "shared"),
                    label="shared",
                ),
            ),
        )


class _TransportResource(DeviceManagerStatusMixin):
    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)


def test_lifecycle_service_disables_reconnect_for_cross_device_shared_transport():
    first_id = HardwareDeviceId("laser", "first:shared")
    second_id = HardwareDeviceId("laser", "second:shared")
    first = _SharedTransportManager(_Lifecycle(first_id), name="first")
    second = _SharedTransportManager(_Lifecycle(second_id), name="second")
    master = _master(lasers={"first": first, "second": second})
    master.rs232sManager = _Group({"shared": _TransportResource()})
    service = DeviceLifecycleService(master, DeviceSupervisor(master))

    assert service.canReconnect(first_id) is False
    assert service.canReconnect(second_id) is False
    with pytest.raises(DeviceLifecycleBlockedError, match="shared"):
        service.reconnect(first_id)


def _rs232_info(port="COM10"):
    return SimpleNamespace(
        managerProperties={
            "port": port,
            "encoding": "ascii",
            "recv_termination": "\r\n",
            "send_termination": "\r",
            "baudrate": 115200,
            "bytesize": 8,
            "parity": "none",
            "stopbits": 1,
            "rtscts": False,
            "dsrdtr": False,
            "xonxoff": False,
        }
    )


def test_rs232_reconnect_replaces_backend_in_place_and_can_heal_io_error(monkeypatch):
    from imswitch.imcontrol.model.interfaces import RS232Driver
    from imswitch.imcontrol.model.managers.rs232.RS232Manager import RS232Manager

    class _FlakyDriver:
        attempts = 0

        def __init__(self, port):
            self.port = port
            self.closed = False

        def initialize(self):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise OSError("port unavailable")

        def query(self, command):
            if command == "FAIL":
                raise OSError("link lost")
            return f"ACK:{command}"

        def write(self, command):
            return None

        def read(self, *args, **kwargs):
            return "READY"

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        RS232Driver, "generateDriverClass", lambda _settings: _FlakyDriver
    )

    manager = RS232Manager(_rs232_info(), "coolLED")
    identity = id(manager)
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR

    assert manager.reconnectTransport() is True
    assert id(manager) == identity
    assert manager.runtimeMode is DeviceRuntimeMode.REAL
    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert manager.query("PING") == "ACK:PING"

    with pytest.raises(OSError, match="link lost"):
        manager.query("FAIL")
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.runtimeMode is DeviceRuntimeMode.REAL

    assert manager.query("PING") == "ACK:PING"
    assert manager.connectionState is DeviceConnectionState.CONNECTED


class _ReconnectableCoolLedTransport(DeviceManagerStatusMixin):
    def __init__(self, *, reconnect_success=True):
        self.commands = []
        self.reconnect_success = reconnect_success
        self._setConnectionError(
            OSError("COM10 unavailable"),
            summary="RS232 transport COM10 failed; mock fallback active",
            mock_active=True,
        )

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    def reconnectTransport(self):
        if self.reconnect_success:
            self._setConnected("RS232 transport COM10 reopened")
            return True
        self._setConnectionError(
            OSError("COM10 still unavailable"),
            summary="RS232 transport COM10 reconnect failed; mock fallback active",
            mock_active=True,
        )
        return False

    def query(self, command):
        if self.runtimeMode is DeviceRuntimeMode.MOCK:
            return None
        self.commands.append(command)
        if command == "CSS?":
            return "CSS status"
        return "OK"


def _coolled_info(channel):
    return SimpleNamespace(
        managerProperties={"rs232device": "coolLED", "channel_index": channel},
        wavelength=405,
        valueRangeMin=0,
        valueRangeMax=100,
        valueRangeStep=1,
        freqRangeMin=None,
        freqRangeMax=None,
        freqRangeInit=None,
    )


def test_coolled_shared_lifecycle_reconnects_once_and_dynamic_mock_state_recovers():
    from imswitch.imcontrol.model.managers.lasers.CoolLEDLaserManager import (
        CoolLEDLaserManager,
    )

    transport = _ReconnectableCoolLedTransport()
    low_level = {"rs232sManager": {"coolLED": transport}}
    first = CoolLEDLaserManager(_coolled_info("A"), "405 LED", **low_level)
    second = CoolLEDLaserManager(_coolled_info("B"), "488 LED", **low_level)

    assert first._isMock is True
    assert second._isMock is True
    lifecycle = first.getDeviceLifecycle()
    assert second.getDeviceLifecycle() is lifecycle

    master = _master(lasers={"405 LED": first, "488 LED": second})
    master.rs232sManager = _Group({"coolLED": transport})
    service = DeviceLifecycleService(master, DeviceSupervisor(master))
    hardware_id = HardwareDeviceId("laser", "coolled:coolLED")
    assert service.canReconnect(hardware_id) is True

    result = service.reconnect(hardware_id)

    assert result.success is True
    assert first._isMock is False
    assert second._isMock is False
    assert first.connectionState is DeviceConnectionState.CONNECTED
    assert second.connectionState is DeviceConnectionState.CONNECTED
    assert transport.commands.count("CSS?") == 1
    assert "CAF" in transport.commands
    assert "CBF" in transport.commands
    assert set(result.deactivated_device_ids) == {
        DeviceId("laser", "405 LED"),
        DeviceId("laser", "488 LED"),
    }

    first.setEnabled(True)
    assert transport.commands[-1] == "CAN"


def test_failed_coolled_reconnect_keeps_mock_fallback_visible():
    from imswitch.imcontrol.model.managers.lasers.CoolLEDLaserManager import (
        CoolLEDLaserManager,
    )

    transport = _ReconnectableCoolLedTransport(reconnect_success=False)
    manager = CoolLEDLaserManager(
        _coolled_info("A"),
        "405 LED",
        rs232sManager={"coolLED": transport},
    )

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert manager._isMock is True
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert "transport unavailable" in result.summary.lower()

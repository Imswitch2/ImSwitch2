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


class _BlockedLifecycle(_Lifecycle):
    def reconnect(self):
        self.calls += 1
        raise DeviceLifecycleBlockedError('device is still in use')


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


def test_lifecycle_service_preserves_adapter_blocked_errors():
    hardware_id = HardwareDeviceId("laser", "managed:blocked")
    lifecycle = _BlockedLifecycle(hardware_id)
    manager = _LifecycleManager(lifecycle, name="managed")
    master = _master(lasers={"managed": manager})
    service = DeviceLifecycleService(master, DeviceSupervisor(master))

    with pytest.raises(DeviceLifecycleBlockedError, match="still in use"):
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
    """A plain transport with no runtime reconnect."""

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)


class _ReopenableTransport(_TransportResource):
    """An rs232devices manager that reopens its port in place."""

    def __init__(self, *, succeed=True):
        self.succeed = succeed
        self.reconnects = 0
        self._setConnectionError(OSError("gone"), summary="port lost")

    def reconnectTransport(self):
        self.reconnects += 1
        if self.succeed:
            self._setConnected("port reopened")
            return True
        self._setConnectionError(OSError("still gone"), summary="port still lost",
                                 mock_active=True)
        return False


class _OnSharedPort(_LifecycleManager):
    """A device on the shared port with no lifecycle adapter of its own."""

    def __init__(self, hardware_id, *, name="device"):
        super().__init__(None, name=name)
        self.hardware_id = hardware_id

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(
            role=DeviceRole.PRIMARY,
            hardware_id=self.hardware_id,
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


class _HookedManager(_OnSharedPort):
    """A dependent with re-initialisation hooks (as MPB / AA have)."""

    def __init__(self, hardware_id, *, name="device", errors=()):
        super().__init__(hardware_id, name=name)
        self.calls = []
        self.errors = list(errors)

    def _lifecycleSafeState(self, *, verified):
        self.calls.append(("safe", verified))
        return []

    def _onTransportReconnected(self, real):
        self.calls.append(("reinit", real))
        return list(self.errors)


FIRST_ID = HardwareDeviceId("laser", "first:shared")
SECOND_ID = HardwareDeviceId("laser", "second:shared")


def _transport_rig(transport, *managers):
    master = _master(lasers={manager.name: manager for manager in managers})
    master.rs232sManager = _Group({"shared": transport})
    return DeviceLifecycleService(master, DeviceSupervisor(master))


def test_reconnecting_one_device_on_a_shared_port_reopens_it_once_for_all():
    """The port is one cable: it is reopened once, and every device on it is
    re-initialised and reported as affected (this replaced the veto)."""
    transport = _ReopenableTransport()
    first = _HookedManager(FIRST_ID, name="first")
    second = _OnSharedPort(SECOND_ID, name="second")      # no hooks: the default
    service = _transport_rig(transport, first, second)

    assert service.canReconnect(FIRST_ID) and service.canReconnect(SECOND_ID)
    assert service.transportOf(FIRST_ID) == DeviceId("rs232", "shared")
    assert set(service.devicesOnTransport(DeviceId("rs232", "shared"))) == {FIRST_ID, SECOND_ID}

    result = service.reconnect(FIRST_ID)

    assert result.success, result
    assert transport.reconnects == 1
    assert first.calls == [("safe", False), ("reinit", True)]
    assert set(result.affected_device_ids) == {DeviceId("laser", "first"), DeviceId("laser", "second")}
    assert set(result.deactivated_device_ids) == set(result.affected_device_ids)
    assert second.connectionState is DeviceConnectionState.CONNECTED   # default hook
    assert "re-initialised" in result.summary


def test_a_failed_port_reopen_marks_every_device_on_it():
    transport = _ReopenableTransport(succeed=False)
    first = _OnSharedPort(FIRST_ID, name="first")
    service = _transport_rig(transport, first)

    result = service.reconnect(FIRST_ID)

    assert not result.success
    assert "did not reconnect" in result.summary
    assert first.connectionState is DeviceConnectionState.ERROR
    assert first.runtimeMode is DeviceRuntimeMode.MOCK          # the transport fell back


def test_a_dependent_that_fails_to_reinitialise_fails_the_reconnect():
    transport = _ReopenableTransport()
    first = _HookedManager(FIRST_ID, name="first", errors=["APC mode not confirmed"])
    service = _transport_rig(transport, first)

    result = service.reconnect(FIRST_ID)

    assert not result.success
    assert "APC mode not confirmed" in result.summary
    assert transport.reconnects == 1


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


@pytest.mark.parametrize("group, key_fn", [("lasers", "laser_key"),
                                           ("positioners", "positioner_key")])
def test_reconnect_is_refused_while_a_run_or_script_holds_the_device(group, key_fn):
    """Review: a reserved Märzhäuser stage refused ordinary moves, yet
    reconnect replaced its backend under the reservation."""
    from imswitch.imcontrol.model import resources

    registry = resources.ResourceRegistry()
    previous = resources.set_resource_registry(registry)
    try:
        hardware_id = HardwareDeviceId("laser", "managed:stage")
        lifecycle = _Lifecycle(hardware_id)
        master = _master()
        setattr(master, f"{group}Manager", _Group({"stage": _LifecycleManager(lifecycle, name="stage")}))
        service = DeviceLifecycleService(master, DeviceSupervisor(master))
        key = getattr(resources, key_fn)("stage")

        run = registry.reserve([key], "laser power LUT")
        with pytest.raises(DeviceLifecycleBlockedError, match="laser power LUT"):
            service.reconnect(hardware_id)
        assert lifecycle.calls == 0

        registry.release(run.token)
        assert service.reconnect(hardware_id).success
        assert lifecycle.calls == 1
        assert registry.in_flight(key) == []             # tickets released
    finally:
        resources.set_resource_registry(previous)


def test_a_reservation_waits_for_a_running_reconnect():
    """While a transition runs it holds the devices: a reservation cannot
    slip in under it."""
    import threading

    from imswitch.imcontrol.model import resources

    registry = resources.ResourceRegistry()
    previous = resources.set_resource_registry(registry)
    try:
        hardware_id = HardwareDeviceId("laser", "managed:one")
        entered, release = threading.Event(), threading.Event()

        class _Slow(_Lifecycle):
            def reconnect(self):
                entered.set()
                release.wait(5)
                return super().reconnect()

        lifecycle = _Slow(hardware_id)
        master = _master(lasers={"one": _LifecycleManager(lifecycle, name="one")})
        service = DeviceLifecycleService(master, DeviceSupervisor(master))
        worker = threading.Thread(target=service.reconnect, args=(hardware_id,))
        worker.start()
        assert entered.wait(2)
        with pytest.raises(resources.ResourceReservedError):
            registry.reserve([resources.laser_key("one")], "script", deadline_s=0.05)
        release.set()
        worker.join(5)
        registry.reserve([resources.laser_key("one")], "script", deadline_s=1.0)
    finally:
        resources.set_resource_registry(previous)

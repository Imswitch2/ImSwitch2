from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceId,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.interfaces import elliptecbus
from imswitch.imcontrol.model.managers.rotators.ElliptecRotatorManager import (
    ElliptecRotatorManager,
)


class _FakeStage:
    def __init__(self, positions=None, *, missing=(), move_failures=None):
        self.positions = dict(positions or {})
        self.missing = set(missing)
        self.move_failures = list(move_failures or [])
        self.closed = False
        self.home_calls = []
        self.move_calls = []
        self.update_calls = 0

    def update_connected_addrs(self):
        self.update_calls += 1

    def get_position(self, addr=None):
        if addr in self.missing:
            raise RuntimeError(f"NAK: address {addr} missing")
        return self.positions.get(addr, 0.0)

    def move_to(self, position, addr=None):
        self.move_calls.append((addr, position))
        if self.move_failures:
            exc = self.move_failures.pop(0)
            raise exc
        self.positions[addr] = float(position)

    def home(self, addr=None):
        self.home_calls.append(addr)
        self.positions[addr] = 0.0

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_shared_buses():
    elliptecbus._SharedElliptecBus._instances.clear()
    yield
    for bus in tuple(elliptecbus._SharedElliptecBus._instances.values()):
        try:
            managers = bus.registered_managers()
            for manager in managers:
                bus.release(manager, manager._addr)
        except Exception:
            pass
    elliptecbus._SharedElliptecBus._instances.clear()


def _info(port="COM20", address=0, *, scale="stage", home=False):
    return SimpleNamespace(
        managerProperties={
            "port": port,
            "address": address,
            "scale": scale,
            "homeOnInit": home,
        }
    )


def test_startup_failure_uses_stateful_mock_and_reconnects_in_place(monkeypatch):
    real_stage = _FakeStage({0: 37.5})
    attempts = 0

    def _open(_port, _scale):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("COM20 unavailable")
        return real_stage

    monkeypatch.setattr(elliptecbus, "_open_elliptec_stage", _open)

    manager = ElliptecRotatorManager(_info(), "HWP")
    identity = id(manager)
    assert manager.isMock is True
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR

    manager.move_abs(14.0)
    assert manager.position == pytest.approx(14.0)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert id(manager) == identity
    assert manager.isMock is False
    assert manager.runtimeMode is DeviceRuntimeMode.REAL
    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert manager.position == pytest.approx(37.5)
    assert real_stage.home_calls == []


def test_shared_bus_keeps_missing_address_mock_and_reconnect_refreshes_all(monkeypatch):
    first_stage = _FakeStage({0: 10.0}, missing={1})
    second_stage = _FakeStage({0: 20.0, 1: 30.0})
    stages = iter((first_stage, second_stage))
    monkeypatch.setattr(
        elliptecbus, "_open_elliptec_stage", lambda _port, _scale: next(stages)
    )

    hwp = ElliptecRotatorManager(_info(address=0), "HWP")
    qwp = ElliptecRotatorManager(_info(address=1), "QWP")

    assert hwp._bus is qwp._bus
    assert hwp.isMock is False
    assert qwp.isMock is True
    assert qwp.connectionState is DeviceConnectionState.ERROR

    result = hwp.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert first_stage.closed is True
    assert hwp.position == pytest.approx(20.0)
    assert qwp.position == pytest.approx(30.0)
    assert hwp.isMock is False
    assert qwp.isMock is False
    assert set(result.affected_device_ids) == {
        DeviceId("rotator", "HWP"),
        DeviceId("rotator", "QWP"),
    }


def test_runtime_transport_failure_falls_entire_shared_bus_back_to_mock(monkeypatch):
    stage = _FakeStage(
        {0: 11.0, 1: 22.0},
        move_failures=[OSError("serial port is closed")] * 6,
    )
    monkeypatch.setattr(
        elliptecbus, "_open_elliptec_stage", lambda _port, _scale: stage
    )
    hwp = ElliptecRotatorManager(_info(address=0), "HWP")
    qwp = ElliptecRotatorManager(_info(address=1), "QWP")

    with pytest.raises(OSError, match="closed"):
        hwp.move_abs(50.0)

    assert hwp.isMock is True
    assert qwp.isMock is True
    assert hwp.position == pytest.approx(11.0)
    assert qwp.position == pytest.approx(22.0)
    assert stage.closed is False  # teardown is deferred off the caller/UI thread

    hwp.move_abs(60.0)
    assert hwp.position == pytest.approx(60.0)
    assert len(stage.move_calls) == 6


def test_transient_naks_retry_without_destroying_real_bus(monkeypatch):
    stage = _FakeStage(
        {0: 5.0},
        move_failures=[RuntimeError("NAK busy"), RuntimeError("NAK busy")],
    )
    monkeypatch.setattr(
        elliptecbus, "_open_elliptec_stage", lambda _port, _scale: stage
    )
    manager = ElliptecRotatorManager(_info(), "HWP")

    manager.move_abs(25.0)

    assert manager.isMock is False
    assert manager.position == pytest.approx(25.0)
    assert len(stage.move_calls) == 3


def test_real_to_real_reconnect_closes_old_stage_before_opening_new(monkeypatch):
    old_stage = _FakeStage({0: 4.0})
    new_stage = _FakeStage({0: 8.0})
    calls = 0

    def _open(_port, _scale):
        nonlocal calls
        calls += 1
        if calls == 1:
            return old_stage
        assert old_stage.closed is True
        return new_stage

    monkeypatch.setattr(elliptecbus, "_open_elliptec_stage", _open)
    manager = ElliptecRotatorManager(_info(), "HWP")

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert manager.position == pytest.approx(8.0)
    assert new_stage.home_calls == []


def test_shared_port_rejects_conflicting_scale(monkeypatch):
    monkeypatch.setattr(
        elliptecbus,
        "_open_elliptec_stage",
        lambda _port, _scale: _FakeStage({0: 0.0}),
    )
    ElliptecRotatorManager(_info(address=0, scale="stage"), "HWP")

    with pytest.raises(ValueError, match="already configured"):
        ElliptecRotatorManager(_info(address=1, scale=360.0), "QWP")


def test_lifecycle_service_exposes_reconnect_for_individual_elliptec_row(monkeypatch):
    from imswitch.imcontrol.model.devices import (
        DeviceLifecycleService,
        DeviceSupervisor,
        HardwareDeviceId,
    )
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager

    class _Group(MultiManager):
        def __init__(self, entries):
            self._subManagers = dict(entries)
            self._shutdownFinalizedSubManagerObjects = []

    stages = iter((OSError("COM20 unavailable"), _FakeStage({0: 42.0})))

    def _open(_port, _scale):
        item = next(stages)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(elliptecbus, "_open_elliptec_stage", _open)
    manager = ElliptecRotatorManager(_info(), "HWP")
    empty = _Group({})
    master = SimpleNamespace(
        detectorsManager=empty,
        lasersManager=empty,
        positionersManager=empty,
        rotatorsManager=_Group({"HWP": manager}),
        flipMirrorsManager=empty,
        rs232sManager=empty,
        slmsManager=empty,
        nidaqManager=None,
        pulseGeneratorManager=None,
        triggerScopeManager=None,
        standManager=None,
        scanExecutionCoordinator=SimpleNamespace(
            activeRunToken=None, activeToken=None
        ),
        recordingManager=SimpleNamespace(record=False),
    )

    supervisor = DeviceSupervisor(master)
    service = DeviceLifecycleService(master, supervisor)
    hardware_id = HardwareDeviceId("rotator", "rotator:HWP")

    assert service.canReconnect(hardware_id) is True
    result = service.reconnect(hardware_id)
    assert result.success is True
    assert manager.position == pytest.approx(42.0)

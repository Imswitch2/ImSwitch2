from types import SimpleNamespace

from imswitch.imcontrol.model.devices import (
    DeviceId,
    DeviceLifecycleAction,
    HardwareDeviceId,
)
from imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF import (
    ThorlabsMFFManager,
)


class _FakeMFFDevice:
    def __init__(self, state=0):
        self.state = int(state)
        self.closed = False
        self.moves = []

    def move_to_state(self, state):
        self.moves.append(int(state))
        self.state = int(state)

    def get_state(self):
        return self.state

    def close(self):
        self.closed = True


def _manager_without_vendor(name="Mirror"):
    info = SimpleNamespace(
        serial_number="MFF001",
        invert=False,
        initial_state=None,
        state_names=None,
        managerProperties={},
    )
    manager = object.__new__(ThorlabsMFFManager)
    manager.name = name
    manager.deviceInfo = info
    manager.serial_number = "MFF001"
    manager.invert = False
    manager.initial_state = None
    manager.state_names = {0: "0", 1: "1"}
    manager._device = None
    manager._connected = False
    manager._last_error = None
    manager._last_state = None
    manager._lifecycle = None
    manager._ThorlabsMFFManager__logger = SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    from imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF import (
        _ThorlabsMFFLifecycle,
    )
    manager._lifecycle = _ThorlabsMFFLifecycle(manager)
    return manager


def test_mff_lifecycle_identity_and_capability():
    manager = _manager_without_vendor()
    lifecycle = manager.getDeviceLifecycle()

    assert lifecycle.hardware_id == HardwareDeviceId(
        "flip_mirror", "flip_mirror:Mirror"
    )
    assert lifecycle.capabilities.reconnect is True


def test_mff_reconnect_reads_actual_state_without_moving(monkeypatch):
    manager = _manager_without_vendor()
    old_device = _FakeMFFDevice(state=0)
    new_device = _FakeMFFDevice(state=1)
    manager._device = old_device
    manager._connected = True

    def fake_connect():
        manager._device = new_device
        manager._connected = True
        manager._last_error = None

    monkeypatch.setattr(manager, "_connect", fake_connect)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.action is DeviceLifecycleAction.RECONNECT
    assert result.success is True
    assert result.affected_device_ids == (DeviceId("flip_mirror", "Mirror"),)
    assert old_device.closed is True
    assert new_device.moves == []
    assert manager.get_cached_state() == 1


def test_mff_reconnect_open_failure_leaves_disconnected(monkeypatch):
    manager = _manager_without_vendor()
    old_device = _FakeMFFDevice(state=0)
    manager._device = old_device
    manager._connected = True

    def fake_connect():
        manager._device = None
        manager._connected = False
        manager._last_error = "device missing"

    monkeypatch.setattr(manager, "_connect", fake_connect)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert result.details == "device missing"
    assert old_device.closed is True
    assert manager.is_connected() is False


def test_mff_reconnect_verification_failure_closes_new_handle(monkeypatch):
    manager = _manager_without_vendor()
    old_device = _FakeMFFDevice(state=0)
    new_device = _FakeMFFDevice(state=1)
    manager._device = old_device
    manager._connected = True

    def fake_connect():
        manager._device = new_device
        manager._connected = True
        manager._last_error = None

    monkeypatch.setattr(manager, "_connect", fake_connect)
    monkeypatch.setattr(new_device, "get_state", lambda: (_ for _ in ()).throw(RuntimeError("query failed")))

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert "query failed" in result.details
    assert old_device.closed is True
    assert new_device.closed is True
    assert manager.is_connected() is False

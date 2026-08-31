from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.interfaces import elliptecbus
from imswitch.imcontrol.model.managers.rotators.ElliptecRotatorManager import (
    ElliptecRotatorManager,
)


class _FakeStage:
    def __init__(self, position=12.5):
        self.position = float(position)
        self.closed = False

    def update_connected_addrs(self):
        pass

    def home(self, addr=None):
        self.position = 0.0

    def move_to(self, position, addr=None):
        self.position = float(position)

    def get_position(self, addr=None):
        return self.position

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_shared_buses():
    elliptecbus._SharedElliptecBus._instances.clear()
    yield
    for bus in tuple(elliptecbus._SharedElliptecBus._instances.values()):
        for manager in bus.registered_managers():
            bus.release(manager, manager._addr)
    elliptecbus._SharedElliptecBus._instances.clear()


def _info():
    return SimpleNamespace(
        managerProperties={
            "port": "COM20",
            "address": 0,
            "scale": "stage",
            "homeOnInit": False,
        }
    )


def test_elliptec_reports_connected_from_cached_startup_state(monkeypatch):
    monkeypatch.setattr(
        elliptecbus,
        "_open_elliptec_stage",
        lambda port, scale: _FakeStage(),
    )

    manager = ElliptecRotatorManager(_info(), "HWP")

    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert manager.runtimeMode is DeviceRuntimeMode.REAL


def test_elliptec_reports_mock_fallback_without_status_probe(monkeypatch):
    monkeypatch.setattr(
        elliptecbus,
        "_open_elliptec_stage",
        lambda port, scale: (_ for _ in ()).throw(OSError("COM20 unavailable")),
    )

    manager = ElliptecRotatorManager(_info(), "HWP")

    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert "COM20 unavailable" in (manager.connectionStatusDetails or "")

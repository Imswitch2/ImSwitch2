from types import SimpleNamespace

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

    def home(self, addr=None):
        self.position = 0.0

    def move_to(self, position, addr=None):
        self.position = float(position)

    def get_position(self, addr=None):
        return self.position

    def close(self):
        pass


class _FakeBus:
    def __init__(self, stage):
        import threading
        self.stage = stage
        self.lock = threading.RLock()
        self.refcount = 0

    def acquire(self):
        self.refcount += 1

    def release(self):
        self.refcount -= 1


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
    bus = _FakeBus(_FakeStage())
    monkeypatch.setattr(
        elliptecbus._SharedElliptecBus,
        "get_bus",
        classmethod(lambda cls, port, scale="stage": bus),
    )

    manager = ElliptecRotatorManager(_info(), "HWP")

    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert manager.runtimeMode is DeviceRuntimeMode.REAL


def test_elliptec_reports_mock_fallback_without_status_probe(monkeypatch):
    elliptecbus.MockElliptecBus._instances.clear()
    monkeypatch.setattr(
        elliptecbus._SharedElliptecBus,
        "get_bus",
        classmethod(
            lambda cls, port, scale="stage": (_ for _ in ()).throw(
                OSError("COM20 unavailable")
            )
        ),
    )

    manager = ElliptecRotatorManager(_info(), "HWP")

    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert "COM20 unavailable" in (manager.connectionStatusDetails or "")

    manager.finalize()
    elliptecbus.MockElliptecBus._instances.clear()

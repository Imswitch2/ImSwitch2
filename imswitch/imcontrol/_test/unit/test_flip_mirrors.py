from imswitch.imcontrol.controller.controllers import FlipMirrorController
from imswitch.imcontrol.model.SetupInfo import FlipMirrorInfo
from imswitch.imcontrol.model.managers.FlipMirrorsManager import FlipMirrorsManager
from imswitch.imcontrol.view.widgets import FlipMirrorWidget


def _mock_info(**overrides):
    values = {
        "managerName": "ThorlabsMFF_mock",
        "serial_number": "MFF001",
        "initial_state": 1,
        "state_names": {"0": "Out", "1": "In"},
    }
    values.update(overrides)
    return FlipMirrorInfo(**values)


def test_flip_mirrors_manager_loads_mock_and_resets():
    manager = FlipMirrorsManager({"Mirror": _mock_info()})

    assert manager.hasDevices()
    assert manager.getAllDeviceNames() == ["Mirror"]

    mirror = manager["Mirror"]
    assert mirror.is_connected()
    assert mirror.get_state() == 1
    assert mirror.get_state_names() == {0: "Out", 1: "In"}

    mirror.move_to(0)
    assert mirror.get_state() == 0

    mirror.close()
    assert not mirror.is_connected()
    assert manager.reset_connections() == {"Mirror": True}
    assert mirror.is_connected()

    manager.finalize()
    assert not mirror.is_connected()


def test_flip_mirror_lazy_exports_are_registered():
    assert FlipMirrorController.__name__ == "FlipMirrorController"
    assert FlipMirrorWidget.__name__ == "FlipMirrorWidget"

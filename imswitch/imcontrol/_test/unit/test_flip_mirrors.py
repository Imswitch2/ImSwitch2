from imswitch.imcontrol.controller.controllers import _CONTROLLER_MODULES
from imswitch.imcontrol.controller.controllers.FlipMirrorController import FlipMirrorController
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


def test_flip_mirrors_manager_loads_mock():
    manager = FlipMirrorsManager({"Mirror": _mock_info()})

    assert manager.hasDevices()
    assert manager.getAllDeviceNames() == ["Mirror"]

    mirror = manager["Mirror"]
    assert mirror.is_connected()
    assert mirror.get_state() == 1
    assert mirror.get_state_names() == {0: "Out", 1: "In"}

    mirror.move_to(0)
    assert mirror.get_state() == 0

    manager.finalize()
    assert not mirror.is_connected()


def test_flip_mirrors_manager_loads_canonical_mock_name():
    manager = FlipMirrorsManager({
        "Mirror": _mock_info(managerName="ThorlabsMFFMockManager")
    })

    mirror = manager["Mirror"]
    assert mirror.__class__.__name__ == "MockThorlabsMFFManager"
    assert mirror.is_connected()

    manager.finalize()


def test_flip_mirrors_manager_loads_canonical_hardware_class():
    from imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF import (
        ThorlabsMFF,
        ThorlabsMFFManager,
    )

    assert ThorlabsMFF is ThorlabsMFFManager


def test_flip_mirror_lazy_exports_are_registered():
    # The package attribute is the class only until some test imports the submodule (the import
    # system then binds the module to the same name), so check the registry and the class itself.
    assert _CONTROLLER_MODULES["FlipMirrorController"] == "FlipMirrorController"
    assert FlipMirrorController.__name__ == "FlipMirrorController"
    assert FlipMirrorWidget.__name__ == "FlipMirrorWidget"

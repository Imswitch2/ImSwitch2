from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.interfaces import LeicaDMIHardware
from imswitch.imcontrol.model.managers.positioners.LeicaDMIZPositionerManager import (
    LeicaDMIZPositionerManager,
)
from imswitch.imcontrol.model.managers.stands.LeicaDMIStandManager import (
    LeicaDMIStandManager,
)


pytestmark = pytest.mark.nohardware


class FakeLeicaRS232:
    def __init__(self):
        self.position_du = 1000
        self.commands = []

    def query(self, cmd):
        self.commands.append(cmd)
        if cmd == "71042":
            return "71042 0.004"
        if cmd == "71023":
            return f"71023 {self.position_du}"
        if cmd == "71056":
            return "71056 100000"
        if cmd.startswith("71024 "):
            self.position_du += int(cmd.split()[1])
            return cmd
        if cmd.startswith("71022 "):
            self.position_du = int(cmd.split()[1])
            return cmd
        return cmd

    def write(self, cmd):
        self.commands.append(cmd)
        return None


class FakeRS232sManager:
    def __init__(self, **devices):
        self._devices = devices

    def __getitem__(self, name):
        return self._devices[name]


@pytest.fixture(autouse=True)
def fake_leica_hardware(monkeypatch):
    monkeypatch.setattr(
        LeicaDMIHardware,
        "_loadPrivateHardwareClass",
        lambda _logger: FakeLeicaHardware,
    )
    with LeicaDMIHardware._CACHE_LOCK:
        LeicaDMIHardware._HARDWARE_CACHE.clear()
    yield
    with LeicaDMIHardware._CACHE_LOCK:
        LeicaDMIHardware._HARDWARE_CACHE.clear()


class FakeLeicaHardware:
    def __init__(self, rs232Manager, *, managerProperties=None, logger=None):
        self._rs232 = rs232Manager
        self.connectionError = None
        self._z_um_per_device_unit = 0.0
        self.configure(managerProperties or {})

    def configure(self, managerProperties):
        response = self._rs232.query("71042")
        self._z_um_per_device_unit = float(response.split()[1])

    def isConnected(self):
        return True

    def has_z_position_um(self):
        return self._z_um_per_device_unit > 0

    def get_z_position_device_units(self):
        return int(self._rs232.query("71023").split()[1])

    def get_z_position_um(self):
        return self.get_z_position_device_units() * self._z_um_per_device_unit

    def move_z_relative_um(self, value):
        steps = int(round(float(value) / self._z_um_per_device_unit))
        self._rs232.query(f"71024 {steps}")
        return self.get_z_position_um()

    def set_z_position_um(self, value):
        steps = int(round(float(value) / self._z_um_per_device_unit))
        self._rs232.query(f"71022 {steps}")
        return self.get_z_position_um()

    def get_pos_nm(self):
        return self.get_z_position_um() * 1000

    def set_pos_nm(self, pos_nm):
        return self.set_z_position_um(float(pos_nm) / 1000)


def _positioner_info():
    return SimpleNamespace(
        axes=["Z"],
        forPositioning=True,
        forScanning=False,
        resetOnClose=False,
        joystick=False,
        liveUpdate=True,
        hide=False,
        shortcutModifier=None,
        managerProperties={"rs232device": "Leica"},
    )


def _stand_info():
    return SimpleNamespace(
        rs232device="Leica",
        managerProperties={"availableCubes": {1: "DAPI", 2: "EMP_BF"}},
    )


def test_stand_and_z_positioner_share_one_hardware_interface():
    rs232 = FakeLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)

    stand = LeicaDMIStandManager(_stand_info(), rs232sManager=rs232s)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    assert stand._hardware is z._hardware
    assert stand.isConnected()
    assert z.isAvailable
    assert stand.connectionError is None
    assert z.connectionError is None


def test_z_positioner_reports_micrometers_and_refreshes_external_motion():
    rs232 = FakeLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    assert z.position["Z"] == pytest.approx(4.0)

    # Simulate a move made outside ImSwitch. Generic PositionerController live
    # polling calls updatePosition(), which must refresh the cached position.
    rs232.position_du = 1750
    z.updatePosition()
    assert z.position["Z"] == pytest.approx(7.0)


def test_z_positioner_uses_physical_units_for_moves():
    rs232 = FakeLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    z.move(2.0, "Z")
    assert z.position["Z"] == pytest.approx(6.0)
    assert rs232.position_du == 1500

    z.setPosition(8.0, "Z")
    assert z.position["Z"] == pytest.approx(8.0)
    assert rs232.position_du == 2000


def test_transient_z_poll_failure_propagates_but_keeps_positioner_available():
    class FailingOnceRS232(FakeLeicaRS232):
        fail_next_position = False

        def query(self, cmd):
            if cmd == "71023" and self.fail_next_position:
                self.fail_next_position = False
                raise RuntimeError("temporary serial loss")
            return super().query(cmd)

    rs232 = FailingOnceRS232()
    rs232s = FakeRS232sManager(Leica=rs232)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    rs232.fail_next_position = True
    with pytest.raises(RuntimeError, match="temporary serial loss"):
        z.updatePosition()

    # Availability stays true so PositionerController's guarded live polling
    # keeps retrying instead of permanently hiding the positioner.
    assert z.isAvailable
    assert z.connectionError == "temporary serial loss"

    z.updatePosition()
    assert z.connectionError is None

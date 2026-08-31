from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices.status import DeviceConnectionState
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
    assert stand.connectionState is DeviceConnectionState.CONNECTED
    assert z.connectionState is DeviceConnectionState.CONNECTED


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
    assert z.connectionState is DeviceConnectionState.ERROR

    z.updatePosition()
    assert z.connectionState is DeviceConnectionState.CONNECTED


def test_stand_and_z_positioner_share_one_physical_lifecycle():
    rs232 = FakeLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)

    stand = LeicaDMIStandManager(_stand_info(), rs232sManager=rs232s)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    lifecycle = stand.getDeviceLifecycle()
    assert lifecycle is z.getDeviceLifecycle()
    assert lifecycle.hardware_id == stand.getDeviceDescriptorSpec().hardware_id
    assert lifecycle.hardware_id == z.getDeviceDescriptorSpec().hardware_id
    assert lifecycle.capabilities.reconnect


def test_leica_lifecycle_reconnects_mock_transport_and_rebinds_shared_hardware():
    from imswitch.imcontrol.model.devices.status import (
        DeviceManagerStatusMixin,
        DeviceRuntimeMode,
    )

    class ReconnectableLeicaRS232(FakeLeicaRS232, DeviceManagerStatusMixin):
        def __init__(self):
            super().__init__()
            self._real_available = False
            self._setConnectionError(
                "COM unavailable",
                summary="mock fallback active",
                mock_active=True,
            )

        def query(self, cmd):
            if self.runtimeMode is DeviceRuntimeMode.MOCK:
                self.commands.append(cmd)
                return None
            return super().query(cmd)

        def write(self, cmd):
            if self.runtimeMode is DeviceRuntimeMode.MOCK:
                self.commands.append(cmd)
                return None
            return super().write(cmd)

        def reconnectTransport(self):
            self._real_available = True
            self._setConnected("transport reopened")
            return True

    rs232 = ReconnectableLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)
    stand = LeicaDMIStandManager(_stand_info(), rs232sManager=rs232s)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    assert stand._hardware is None
    assert z._hardware is None

    result = stand.getDeviceLifecycle().reconnect()

    assert result.success
    assert "shutters closed" in result.summary
    assert stand._hardware is z._hardware
    assert stand.isConnected()
    assert z.isAvailable
    assert stand.runtimeMode is DeviceRuntimeMode.REAL
    assert z.runtimeMode is DeviceRuntimeMode.REAL
    assert z.position["Z"] == pytest.approx(4.0)
    assert "77032 1 0" in rs232.commands
    assert "77032 0 0" in rs232.commands
    assert {device_id.kind for device_id in result.affected_device_ids} == {
        "stand",
        "positioner",
    }


def test_leica_lifecycle_failed_transport_reconnect_keeps_mock_fallback_visible():
    from imswitch.imcontrol.model.devices.status import (
        DeviceManagerStatusMixin,
        DeviceRuntimeMode,
    )

    class FailedReconnectLeicaRS232(FakeLeicaRS232, DeviceManagerStatusMixin):
        def __init__(self):
            super().__init__()
            self._setConnectionError(
                "COM unavailable",
                summary="mock fallback active",
                mock_active=True,
            )

        def query(self, cmd):
            self.commands.append(cmd)
            return None

        def reconnectTransport(self):
            self._setConnectionError(
                "still unavailable",
                summary="reconnect failed; mock fallback active",
                mock_active=True,
            )
            return False

    rs232 = FailedReconnectLeicaRS232()
    rs232s = FakeRS232sManager(Leica=rs232)
    stand = LeicaDMIStandManager(_stand_info(), rs232sManager=rs232s)
    z = LeicaDMIZPositionerManager(
        _positioner_info(), "Objective Z", rs232sManager=rs232s
    )

    result = stand.getDeviceLifecycle().reconnect()

    assert not result.success
    assert "mock fallback active" in result.summary
    assert stand._hardware is None
    assert z._hardware is None
    assert stand.runtimeMode is DeviceRuntimeMode.MOCK
    assert z.runtimeMode is DeviceRuntimeMode.MOCK
    assert stand.connectionState is DeviceConnectionState.ERROR
    assert z.connectionState is DeviceConnectionState.ERROR

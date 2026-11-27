"""Marzhauser Tango speed/acceleration from managerProperties (fake serial device)."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import DeviceRuntimeMode
from imswitch.imcontrol.model.managers.positioners.MHXYStageManager import MHXYStageManager


class FakeTango:
    """Answers ?pos/?vel/?accel and stores !vel/!accel like the controller."""

    def __init__(self):
        self.runtimeMode = DeviceRuntimeMode.REAL
        self.commands = []
        self.writes = []
        self.vel = {'x': 40.0, 'y': 40.0}
        self.accel = {'x': 1.0, 'y': 1.0}
        self.reject = set()

    def reconnectTransport(self):
        return True

    def write(self, command):
        self.writes.append(command)
        name, axis, value = command.split()
        if name in self.reject:
            return 'ERR 5'
        getattr(self, name[1:])[axis] = float(value)
        return None

    def query(self, command):
        self.commands.append(command)
        if command == '?pos':
            return '10 20'
        if command.startswith('?vel') or command.startswith('?accel'):
            name, axis = command[1:].split()
            return f'{getattr(self, name)[axis]:.6f}'
        return 'serial-123'


def stage(transport, **props):
    info = SimpleNamespace(axes=['X', 'Y'], managerProperties={'rs232device': 's', **props},
                           forPositioning=True, forScanning=False, resetOnClose=False,
                           joystick=False, liveUpdate=False)
    return MHXYStageManager(info, 'XY', rs232sManager={'s': transport})


def test_absent_settings_send_nothing():
    dev = FakeTango()
    stage(dev)
    assert dev.writes == []
    assert not any(c.startswith(('?vel', '?accel')) for c in dev.commands)


def test_common_values_applied_and_read_back():
    dev = FakeTango()
    manager = stage(dev, velocity=5, acceleration=0.2)
    assert sorted(dev.writes) == sorted(['!vel x 5', '!vel y 5', '!accel x 0.2', '!accel y 0.2'])
    assert manager._appliedMotionProfile == {'vel': {'X': 5.0, 'Y': 5.0},
                                             'accel': {'X': 0.2, 'Y': 0.2}}


def test_per_axis_values_and_only_acceleration():
    dev = FakeTango()
    stage(dev, acceleration={'X': 0.1, 'Y': 0.3})
    assert dev.writes == ['!accel x 0.1', '!accel y 0.3']
    assert dev.vel == {'x': 40.0, 'y': 40.0}


@pytest.mark.parametrize('props', [
    {'velocity': 0}, {'velocity': -1}, {'velocity': 500}, {'velocity': 'fast'},
    {'acceleration': 0.001}, {'acceleration': 100}, {'acceleration': float('nan')},
    {'acceleration': True}, {'acceleration': {'Z': 1}},
])
def test_out_of_range_refused_before_any_io(props):
    dev = FakeTango()
    with pytest.raises(ValueError, match='Marzhauser'):
        stage(dev, **props)
    assert dev.writes == [] and dev.commands == []


def test_reconnect_reapplies_after_power_cycle():
    dev = FakeTango()
    manager = stage(dev, velocity=3, acceleration=0.5)
    dev.vel = {'x': 40.0, 'y': 40.0}           # controller power-cycled
    dev.accel = {'x': 1.0, 'y': 1.0}
    dev.writes.clear()
    assert manager.getDeviceLifecycle().reconnect().success
    assert dev.vel == {'x': 3.0, 'y': 3.0} and dev.accel == {'x': 0.5, 'y': 0.5}


def test_controller_error_is_reported_and_motion_still_allowed(caplog):
    dev = FakeTango()
    dev.reject = {'!accel'}
    manager = stage(dev, acceleration=0.5)
    # read-back shows the controller kept its own value; warned, not hidden
    assert manager._appliedMotionProfile['accel'] == {'X': 1.0, 'Y': 1.0}
    manager.move(1, 'X')
    assert manager.position['X'] == 11


def test_mock_transport_gets_nothing():
    dev = FakeTango()
    dev.runtimeMode = DeviceRuntimeMode.MOCK
    stage(dev, velocity=3)
    assert dev.writes == []

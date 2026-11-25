import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.KDC101PositionerManager import (
    KDC101PositionerManager,
)


class _FakeKDC101:
    def __init__(self, initial_position=0):
        self.status = {'position': initial_position}
        self.jogparams = {
            'step_size': 0,
            'acceleration': 0,
            'max_velocity': 0,
        }
        self.velparams = {
            'acceleration': 0,
            'max_velocity': 0,
        }
        self.move_absolute_calls = []
        self.velocity_calls = []
        self.stop_calls = 0
        self.closed = False

    def move_absolute(self, position):
        self.move_absolute_calls.append(position)
        self.status['position'] = position

    def move_velocity(self, forward):
        self.velocity_calls.append(forward)

    def stop(self):
        self.stop_calls += 1

    def home(self):
        self.status['position'] = 0

    def close(self):
        self.closed = True

    def set_jog_params(self, step_size, acceleration, max_velocity):
        self.jogparams.update({
            'step_size': step_size,
            'acceleration': acceleration,
            'max_velocity': max_velocity,
        })

    def set_velocity_params(self, acceleration, max_velocity):
        self.velparams.update({
            'acceleration': acceleration,
            'max_velocity': max_velocity,
        })


def _positioner_info(manager_properties, axes=None):
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='KDC101PositionerManager',
        managerProperties=manager_properties,
        axes=axes or ['R'],
        forPositioning=True,
        resetOnClose=False,
        liveUpdate=True,
    )


def test_kdc101_positioner_uses_configured_position_units(monkeypatch):
    device = _FakeKDC101(initial_position=100)
    monkeypatch.setattr(
        KDC101PositionerManager,
        '_getDeviceObj',
        lambda self, port: device,
    )

    manager = KDC101PositionerManager(
        _positioner_info({
            'port': 'COM15',
            'posConvFac': 10,
            'velConvFac': 2,
            'accConvFac': 4,
            'positionUnit': 'deg',
        }),
        'Rotation stage',
    )

    manager.setPosition(12.5, 'R')
    manager.move(0.5, 'R')

    assert manager.positionUnit == 'deg'
    assert device.move_absolute_calls == [125, 130]
    assert manager.position['R'] == 13.0


def test_kdc101_positioner_rejects_multiple_axes():
    with pytest.raises(RuntimeError, match='only supports one axis'):
        KDC101PositionerManager(
            _positioner_info({
                'port': 'COM15',
                'posConvFac': 10,
                'velConvFac': 2,
                'accConvFac': 4,
            }, axes=['X', 'Y']),
            'KDC',
        )


def test_kdc101_positioner_rejects_zero_position_scaling(monkeypatch):
    monkeypatch.setattr(
        KDC101PositionerManager,
        '_getDeviceObj',
        lambda self, port: (_ for _ in ()).throw(
            AssertionError('hardware should not be touched for invalid scaling')
        ),
    )

    with pytest.raises(ValueError, match='posConvFac'):
        KDC101PositionerManager(
            _positioner_info({
                'port': 'COM15',
                'posConvFac': 0,
                'velConvFac': 2,
                'accConvFac': 4,
            }),
            'KDC',
        )


def test_kdc101_positioner_exposes_legacy_velocity_helpers(monkeypatch):
    device = _FakeKDC101()
    monkeypatch.setattr(
        KDC101PositionerManager,
        '_getDeviceObj',
        lambda self, port: device,
    )

    manager = KDC101PositionerManager(
        _positioner_info({
            'port': 'COM15',
            'posConvFac': 10,
            'velConvFac': 2,
            'accConvFac': 4,
        }),
        'KDC',
    )

    manager.setJogDistanceInUnits(1.5)
    manager.setJogVelocityInUnits(3.0)
    manager.setMoveAccelerationInUnits(2.0)
    manager.jog_start('R', -1)
    manager.jog_stop('R')
    manager.finalize()

    assert device.jogparams['step_size'] == 15
    assert device.jogparams['max_velocity'] == 6
    assert device.velparams['acceleration'] == 8
    assert device.velocity_calls == [False]
    assert device.stop_calls == 1
    assert device.closed is True


# ---------------------------------------------------------------- failure policy
def _failing_then_working(monkeypatch, device):
    """_getDeviceObj raises until ``state['works']`` is set, then returns ``device``."""
    state = {'works': False}

    def getDeviceObj(self, port):
        if not state['works']:
            raise OSError(f'{port} could not be opened')
        return device

    monkeypatch.setattr(KDC101PositionerManager, '_getDeviceObj', getDeviceObj)
    return state


def test_kdc101_positioner_that_cannot_open_is_absent_refuses_commands_and_reconnects(monkeypatch):
    from imswitch.imcontrol.model.devices import (
        DeviceConnectionState, DeviceNotConnectedError, DeviceRuntimeMode,
    )

    device = _FakeKDC101(initial_position=100)
    homed = []
    device.home = lambda: homed.append(True)
    state = _failing_then_working(monkeypatch, device)

    manager = KDC101PositionerManager(
        _positioner_info({'port': 'COM15', 'posConvFac': 10, 'velConvFac': 2,
                          'accConvFac': 4, 'homeOnInit': True}),
        'Rotation stage',
    )

    assert manager.runtimeMode is DeviceRuntimeMode.REAL
    assert manager.connectionState is DeviceConnectionState.ERROR and not manager.isUsable
    assert 'COM15' in manager.connectionStatusDetails
    with pytest.raises(DeviceNotConnectedError):
        manager.move(1.0, 'R')
    with pytest.raises(DeviceNotConnectedError):
        manager.setPosition(1.0, 'R')
    with pytest.raises(DeviceNotConnectedError):
        manager.jog_start('R', 1)
    assert device.move_absolute_calls == [] and homed == []     # nothing reached hardware

    state['works'] = True
    result = manager.getDeviceLifecycle().reconnect()
    assert result.success, result.summary
    assert manager.isUsable and manager.position['R'] == 10.0   # a fresh read
    assert device.move_absolute_calls == []                    # a reconnect moves nothing
    assert homed == []                                         # and never homes
    manager.move(1.0, 'R')
    assert device.move_absolute_calls == [110]


def test_kdc101_rs232_manager_that_cannot_open_is_absent_not_silent(monkeypatch):
    from types import SimpleNamespace

    import imswitch.imcontrol.model.managers.rs232.KDC101Manager as module
    from imswitch.imcontrol.model.devices import DeviceNotConnectedError, DeviceRuntimeMode

    class Device:
        works = False

        def __init__(self, serial_port):
            if not Device.works:
                raise OSError(f'{serial_port} busy')
            self.calls = []

        def home(self):
            self.calls.append('home')

        def close(self):
            pass

    monkeypatch.setattr(module, 'KDC101', Device, raising=False)
    monkeypatch.setattr(module, '_KDC101_AVAILABLE', True)
    info = SimpleNamespace(managerProperties={'port': 'COM3', 'posConvFac': 1,
                                              'velConvFac': 1, 'accConvFac': 1})
    manager = module.KDC101Manager(info)

    assert manager.runtimeMode is DeviceRuntimeMode.REAL and not manager.isUsable
    assert manager.device_active is False
    with pytest.raises(DeviceNotConnectedError, match='KDC101 on COM3'):
        manager.home()                              # was a silent return
    with pytest.raises(DeviceNotConnectedError):
        manager.getPositionInUnits()                # was a silent 0.0

    Device.works = True
    assert manager.reconnectTransport() is True
    assert manager.device_active is True
    manager.home()
    assert manager._device.calls == ['home']        # only the user's own command

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

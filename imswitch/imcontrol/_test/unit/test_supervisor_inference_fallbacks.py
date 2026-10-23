"""Passive status inference: fallbacks must never look real or configured-mock."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin
from imswitch.imcontrol.model.devices.supervisor import resolveDeviceStatus


def _status(manager):
    return resolveDeviceStatus(DeviceId('x', 'dev'), manager)


class _Plain:
    pass


def test_an_unplugged_kinesis_is_a_real_device_that_is_not_connected():
    """P-2 (device-reconnect-2.0.md): no fallback mock. The rotator keeps
    its real mode, shows the error, and refuses commands until reconnected;
    ``useMockOnFailure: true`` is the explicit opt-in for the old fallback."""
    from imswitch.imcontrol.model.devices import DeviceNotConnectedError
    from imswitch.imcontrol.model.managers.rotators.KinesisRotatorManager import (
        KinesisRotatorManager,
    )
    manager = KinesisRotatorManager(
        SimpleNamespace(managerProperties={'snr': '55000000'}), 'k10')
    status = _status(manager)            # no hardware here
    assert status.mode is DeviceRuntimeMode.REAL
    assert status.connection is DeviceConnectionState.ERROR
    assert manager.isSimulated is True   # a calibration run still refuses it
    with pytest.raises(DeviceNotConnectedError):
        manager.move_abs(10.0)

    fallback = KinesisRotatorManager(
        SimpleNamespace(managerProperties={'snr': '55000000', 'useMockOnFailure': True}), 'k10')
    assert _status(fallback).mode is DeviceRuntimeMode.MOCK
    fallback.move_abs(10.0)              # the mock takes it


def test_wrapped_motor_mock_is_detected():
    manager = _Plain()
    manager._motor = type('MockStandaMotor', (), {})()
    assert _status(manager).mode is DeviceRuntimeMode.MOCK


def test_fallback_flag_wins_over_an_earlier_false_flag():
    manager = _Plain()
    manager.mockermode = False
    manager._mock_fallback = True
    status = _status(manager)
    assert status.mode is DeviceRuntimeMode.MOCK
    assert status.connection is DeviceConnectionState.ERROR


def test_recorded_connect_error_is_an_error_not_disconnected():
    manager = _Plain()
    manager._connected = False
    manager._last_error = 'pylablib is not installed'
    status = _status(manager)
    assert status.connection is DeviceConnectionState.ERROR
    assert status.details == 'pylablib is not installed'
    plain = _Plain()
    plain._connected = False
    assert _status(plain).connection is DeviceConnectionState.DISCONNECTED


def test_device_active_false_is_reported():
    manager = _Plain()
    manager.device_active = False
    assert _status(manager).connection is DeviceConnectionState.DISCONNECTED


def test_mixin_status_is_read_as_one_snapshot():
    class Managed(DeviceManagerStatusMixin):
        pass

    manager = Managed()
    manager._setConnectionError('USB lost', summary='Hardware connection failed',
                                mock_active=True)
    # A torn write in progress: new state set, details not yet updated.
    manager._deviceConnectionState = DeviceConnectionState.CONNECTED
    status = _status(manager)
    assert status.connection is DeviceConnectionState.ERROR
    assert status.details == 'USB lost'
    manager._setConnected('back')
    status = _status(manager)
    assert (status.connection, status.details) == (DeviceConnectionState.CONNECTED, None)


def test_stand_fallback_marks_its_sub_manager(monkeypatch):
    from imswitch.imcontrol.model.managers.StandManager import StandManager

    class FallbackStand:
        def __init__(self, deviceInfo, **kw):
            pass

    monkeypatch.setattr(StandManager, '_resolveStandManagerClass',
                        classmethod(lambda cls, pkg, name, logger: (FallbackStand, True)))
    wrapper = StandManager(SimpleNamespace(managerName='LeicaDMIStandManager'))
    assert wrapper._subManager._mock_fallback is True
    assert _status(wrapper._subManager).connection is DeviceConnectionState.ERROR

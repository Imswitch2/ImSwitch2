"""P-2 (device-reconnect-2.0.md §4.0): a device's mode comes from the setup
and never changes at runtime; a failure changes its connection state, and a
real device that is not connected refuses commands instead of pretending."""
import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceManagerStatusMixin,
    DeviceNotConnectedError,
    DeviceRuntimeMode,
)


class _Manager(DeviceManagerStatusMixin):
    name = 'dev'


def test_a_legacy_manager_that_records_nothing_is_usable():
    assert _Manager().isUsable
    _Manager()._requireConnected()


def test_a_connected_real_device_is_usable_and_a_failed_one_refuses():
    m = _Manager()
    m._setConnected('ok')
    assert m.isUsable and m.runtimeMode is DeviceRuntimeMode.REAL
    m._setConnectionError(OSError('USB gone'), summary='stage lost')
    assert not m.isUsable
    with pytest.raises(DeviceNotConnectedError, match='dev: move refused, stage lost'):
        m._requireConnected('move')
    m._setDisconnected('finalized')
    assert not m.isUsable


def test_an_absent_transient_device_refuses_until_connected():
    m = _Manager()
    m._setAbsent()
    assert m.runtimeMode is DeviceRuntimeMode.ABSENT
    assert m.connectionState is DeviceConnectionState.DISCONNECTED
    with pytest.raises(DeviceNotConnectedError, match='Not connected'):
        m._requireConnected()
    m._setConnected('plugged in')
    assert m.isUsable


def test_a_configured_mock_is_always_usable_and_keeps_its_mode():
    m = _Manager()
    m._setMockActive('mock setup')
    assert m.isUsable
    m._setConnectionError(OSError('x'), summary='a mock does not fail', mock_active=True)
    assert m.runtimeMode is DeviceRuntimeMode.MOCK and m.isUsable
    m._requireConnected()


def test_setup_info_declares_transient_for_any_device():
    from imswitch.imcontrol.model.SetupInfo import (
        DetectorInfo, InstrumentInfo, LaserInfo, PositionerInfo,
    )

    assert LaserInfo(managerName='X', valueRangeMin=0, valueRangeMax=1,
                     wavelength=488).transient is False
    stage = PositionerInfo(managerName='X', axes=['X'], transient=True,
                           connectOnStartup=True)
    assert stage.transient and stage.connectOnStartup
    assert DetectorInfo(managerName='X', transient=True).transient
    assert InstrumentInfo(managerName='X').transient is False

"""Lasers that cannot start are absent, not mocks (device-failure policy).

The ESP32 LED, Pulse Streamer and Python Microscope lasers used to turn into a
silent mock that swallowed every command. Now: a laser that is not there is
reported, refuses commands by name, and a reconnect brings it back dark;
``useMockOnFailure: true`` is the explicit opt-in for a reported mock.
"""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceManagerStatusMixin,
    DeviceNotConnectedError,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.devices.status import backend_attribute
from imswitch.imcontrol.model.SetupInfo import LaserInfo


def _info(managerName, **props):
    return LaserInfo(analogChannel=None, digitalLine=None, managerName=managerName,
                     managerProperties=props, valueRangeMin=0, valueRangeMax=100,
                     wavelength=488)


# ---------------------------------------------------------------- ESP32 LED
class _Squid:
    def __init__(self, sent):
        self.sent = sent

    def set_laser(self, channel, value):
        self.sent.append((channel, value))


class _Board(DeviceManagerStatusMixin):
    """A board with a backend holder, like SQUIDManager."""

    def __init__(self, opens=True):
        self.opens = opens
        self.sent = []
        self._installBackend(self._open, label='SQUID board')

    def _open(self):
        if not self.opens:
            raise OSError('board not plugged in')
        return _Squid(self.sent)

    _squid = backend_attribute()

    def reconnectTransport(self):
        return self._replaceBackend()


def _esp_laser(board, **props):
    from imswitch.imcontrol.model.managers.lasers.ESP32LEDLaserManager import ESP32LEDLaserManager

    return ESP32LEDLaserManager(
        _info('ESP32LEDLaserManager', rs232device='b', channel_index=2, **props), 'LED',
        rs232sManager={'b': board})


def test_esp32_led_on_an_absent_board_refuses_commands_and_is_not_usable():
    board = _Board(opens=False)
    laser = _esp_laser(board)
    assert laser._isMock is False                       # no latch
    assert not laser.isUsable                           # the board it talks through is down
    with pytest.raises(DeviceNotConnectedError):
        laser.setEnabled(True)
    assert laser.enabled is False and board.sent == []


def test_esp32_led_comes_back_dark_after_the_board_reconnects():
    board = _Board(opens=False)
    laser = _esp_laser(board)
    board.opens = True
    assert board.reconnectTransport() is True
    assert laser._onTransportReconnected(True) == []
    assert laser.isUsable and laser.connectionState is DeviceConnectionState.CONNECTED
    assert board.sent == [(2, 0)]                       # off; nothing switched on
    laser.setValue(60)                                  # a power change must not light it
    assert board.sent == [(2, 0)]
    laser.setEnabled(True)
    assert board.sent[-1] == (2, 60)


def test_esp32_led_forgets_a_stale_enabled_state_when_the_board_was_reset():
    board = _Board()
    laser = _esp_laser(board)
    laser.setValue(30)
    laser.setEnabled(True)
    assert laser.enabled and board.sent[-1] == (2, 30)
    board.reconnectTransport()
    laser._onTransportReconnected(True)
    assert laser.enabled is False and board.sent[-1] == (2, 0)
    laser.setValue(80)
    assert board.sent[-1] == (2, 0)


def test_esp32_led_missing_rs232_device_is_an_error_unless_opted_in():
    from imswitch.imcontrol.model.managers.lasers.ESP32LEDLaserManager import ESP32LEDLaserManager

    with pytest.raises(RuntimeError, match="'b' is not available|b.*not available"):
        ESP32LEDLaserManager(_info('ESP32LEDLaserManager', rs232device='b', channel_index=1),
                             'LED', rs232sManager={})
    mocked = ESP32LEDLaserManager(
        _info('ESP32LEDLaserManager', rs232device='b', channel_index=1, useMockOnFailure=True),
        'LED', rs232sManager={})
    mocked.setEnabled(True)
    assert mocked.runtimeMode is DeviceRuntimeMode.MOCK
    assert mocked.connectionState is DeviceConnectionState.ERROR


# ------------------------------------------------------------ Pulse Streamer
def test_pulse_streamer_laser_without_a_streamer_is_not_available_not_a_silent_mock():
    from imswitch.imcontrol.model.managers.lasers.PulseStreamerLaserManager import (
        PulseStreamerLaserManager,
    )

    with pytest.raises(RuntimeError, match="not available.*PulseGeneratorLaserManager"):
        PulseStreamerLaserManager(_info('PulseStreamerLaserManager'), 'PS')
    mocked = PulseStreamerLaserManager(
        _info('PulseStreamerLaserManager', useMockOnFailure=True), 'PS')
    mocked.setEnabled(True)                              # dropped, and reported as a mock
    assert mocked.runtimeMode is DeviceRuntimeMode.MOCK
    assert mocked.connectionState is DeviceConnectionState.ERROR


# ----------------------------------------------------------- Python Microscope
class _Light:
    instances = []
    fail = True

    def __init__(self, port):
        if _Light.fail:
            raise OSError(f'{port} not available')
        self.calls = []
        self._power = None
        _Light.instances.append(self)

    def enable(self):
        self.calls.append('enable')

    def disable(self):
        self.calls.append('disable')

    def shutdown(self):
        self.calls.append('shutdown')

    @property
    def power(self):
        return self._power

    @power.setter
    def power(self, value):
        self.calls.append(('power', value))
        self._power = value


@pytest.fixture
def microscope_light(monkeypatch):
    from imswitch.imcontrol.model.managers.lasers import PyMicroscopeLaserManager as module

    _Light.instances = []
    _Light.fail = True
    package = SimpleNamespace(Light=_Light)
    monkeypatch.setattr(module.importlib, 'import_module', lambda path: package)
    return module


def _pymicroscope(module, **props):
    return module.PyMicroscopeLaserManager(
        _info('PyMicroscopeLaserManager', pyMicroscopeDriver='vendor.Light',
              digitalPorts='COM9', **props), 'PM')


def test_pymicroscope_laser_that_is_not_there_is_absent_and_comes_back_dark(microscope_light):
    laser = _pymicroscope(microscope_light)
    assert laser.runtimeMode is DeviceRuntimeMode.REAL
    assert laser.connectionState is DeviceConnectionState.ERROR and not laser.isUsable
    with pytest.raises(DeviceNotConnectedError):
        laser.setEnabled(True)
    with pytest.raises(DeviceNotConnectedError):
        laser.setValue(10)

    _Light.fail = False
    result = laser.getDeviceLifecycle().reconnect()
    assert result.success, result.summary
    assert laser.isUsable and laser.runtimeMode is DeviceRuntimeMode.REAL
    real = _Light.instances[-1]
    assert 'disable' in real.calls                      # verified dark
    assert 'enable' not in real.calls                   # nothing switched on
    assert not any(isinstance(call, tuple) for call in real.calls)   # no power restored


def test_pymicroscope_laser_mock_is_an_explicit_reported_opt_in(microscope_light):
    laser = _pymicroscope(microscope_light, useMockOnFailure=True)
    assert laser.runtimeMode is DeviceRuntimeMode.MOCK
    assert laser.connectionState is DeviceConnectionState.ERROR and laser.isUsable
    laser.setEnabled(True)
    laser.setValue(5)
    _Light.fail = False
    assert laser.getDeviceLifecycle().reconnect().success
    assert laser.runtimeMode is DeviceRuntimeMode.REAL    # the mock is replaced

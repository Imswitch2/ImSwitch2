"""No-hardware tests for PIStageManager runtime communication hardening."""
from types import SimpleNamespace

import pytest

import imswitch.imcontrol.model.managers.positioners.PIStageManager as pi_module
from imswitch.imcontrol.model.managers.positioners.PIStageManager import PIStageManager

pytestmark = pytest.mark.nohardware


class _TimeoutSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _FakeTimer:
    def __init__(self):
        self.timeout = _TimeoutSignal()
        self.started = []
        self.stopped = False

    def start(self, interval=None):
        self.started.append(interval)

    def stop(self):
        self.stopped = True


class _FakeGCSDevice:
    created = []
    events = []

    def __init__(self, device):
        self.device = device
        self.label = f'D{len(self.created)}'
        self.created.append(self)
        self._timeout = 7000
        self.dcid = 42
        self.fail_qpos = False
        self.fail_qjbs = False
        self.position_mm = 0.001 if self.label == 'D0' else 0.002
        self.joystick_enabled = True

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self.events.append(('timeout', self.label, value))
        self._timeout = value

    def OpenUSBDaisyChain(self, description):
        self.events.append(('open', self.label, description))

    def ConnectDaisyChainDevice(self, device_id, daisychainid):
        self.events.append(('connect', self.label, device_id, daisychainid))

    def qTMN(self):
        return {'1': 0.0}

    def qTMX(self):
        return {'1': 25.0}

    def qPOS(self, axis):
        if self.fail_qpos:
            raise OSError(f'{self.label} qPOS timeout')
        return {1: self.position_mm}

    def GetInterfaceDescription(self):
        return self.label

    def qIDN(self):
        return 'fake PI'

    def VEL(self, axis, speed):
        self.events.append(('vel', self.label, speed))

    def MOV(self, axis, position):
        self.events.append(('mov', self.label, position))

    def qJON(self):
        return {1: self.joystick_enabled}

    def JON(self, axis, enabled):
        self.joystick_enabled = bool(enabled)
        self.events.append(('jon', self.label, bool(enabled)))

    def qJBS(self, joystick, button):
        if self.fail_qjbs:
            raise OSError(f'{self.label} qJBS timeout')
        return {1: {1: False}}

    def HasqONT(self):
        return True

    def qONT(self, axis):
        return {1: True}

    def HasIsMoving(self):
        return False

    def CloseDaisyChain(self):
        self.events.append(('close', self.label))


@pytest.fixture
def fake_pi(monkeypatch):
    _FakeGCSDevice.created = []
    _FakeGCSDevice.events = []
    monkeypatch.setattr(pi_module, 'GCSDevice', _FakeGCSDevice)
    monkeypatch.setattr(pi_module, 'QTimer', _FakeTimer)
    monkeypatch.setattr(
        PIStageManager,
        '_resolve_usb_description',
        lambda self, manager_properties: 'PI USB fake',
    )

    def startup(device):
        _FakeGCSDevice.events.append(('startup', device.label))

    monkeypatch.setattr(pi_module.gcs2pitools, 'startup', startup)
    return _FakeGCSDevice


def _info(runtime_timeout_ms=None):
    props = {'device': 'C-663.11'}
    if runtime_timeout_ms is not None:
        props['runtime_timeout_ms'] = runtime_timeout_ms
    return SimpleNamespace(
        managerProperties=props,
        axes=['X', 'Y'],
        forPositioning=True,
        forScanning=False,
        resetOnClose=False,
        joystick=True,
        liveUpdate=True,
        hide=False,
        shortcutModifier=None,
    )


def test_runtime_timeout_defaults_to_500_and_is_applied_after_startup(fake_pi):
    manager = PIStageManager(_info(), 'PI')

    assert manager.runtimeTimeoutMs == 500
    assert manager.X.timeout == 500
    assert manager.Y.timeout == 500

    events = fake_pi.events
    last_startup = max(i for i, event in enumerate(events) if event[0] == 'startup')
    first_timeout = min(i for i, event in enumerate(events) if event[0] == 'timeout')
    assert last_startup < first_timeout


def test_runtime_timeout_is_configurable(fake_pi):
    manager = PIStageManager(_info(750), 'PI')

    assert manager.X.timeout == 750
    assert manager.Y.timeout == 750


def test_runtime_timeout_must_be_positive_integer(fake_pi):
    with pytest.raises(ValueError, match='runtime_timeout_ms'):
        PIStageManager(_info(0), 'PI')


def test_update_position_is_atomic_when_second_axis_fails(fake_pi):
    manager = PIStageManager(_info(), 'PI')
    manager._position['X'] = 10.0
    manager._position['Y'] = 20.0
    manager.X.position_mm = 0.123
    manager.Y.fail_qpos = True

    with pytest.raises(OSError, match='qPOS timeout'):
        manager.updatePosition()

    assert manager.position == {'X': 10.0, 'Y': 20.0}


def test_movement_status_communication_error_propagates(fake_pi):
    manager = PIStageManager(_info(), 'PI')
    manager.X.qONT = lambda axis: (_ for _ in ()).throw(OSError('qONT timeout'))

    with pytest.raises(OSError, match='qONT timeout'):
        manager.isMovementFinished('X')


def test_button_polling_backs_off_and_recovers(fake_pi):
    manager = PIStageManager(_info(), 'PI')
    manager.buttonTimer.started.clear()
    manager.X.fail_qjbs = True

    manager._pollButtons()

    assert manager._buttonPollFailureCount == 1
    assert manager.buttonTimer.started[-1] == 1000

    manager.X.fail_qjbs = False
    manager._pollButtons()

    assert manager._buttonPollFailureCount == 0
    assert manager.buttonTimer.started[-1] == manager.buttonPollIntervalMs

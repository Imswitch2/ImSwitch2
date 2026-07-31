"""Binary-mask capture owns streaming, laser and timeout cleanup."""

import numpy as np

from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.model.EventTriggeredSession import (
    EventTriggeredSessionState,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)


class _Button:
    def __init__(self):
        self.text = ''

    def setText(self, text):
        self.text = text


class _Selector:
    def currentIndex(self):
        return 0


class _Widget:
    fastImgLasers = ['LASER']
    fastImgDetectors = ['CAM']

    def __init__(self):
        self.fastImgLasersPar = _Selector()
        self.fastImgDetectorsPar = _Selector()
        self.recordBinaryMaskButton = _Button()


class _DetectorManager:
    def __init__(self, fail=False):
        self.fail = fail
        self.acquired = []
        self.released = []

    def acquire(self, names, purpose):
        if self.fail:
            raise RuntimeError('camera unavailable')
        self.acquired.append((tuple(names), purpose))
        return 'mask-handle'

    def release(self, handle):
        self.released.append(handle)


class _Laser:
    def __init__(self):
        self.enabled = []

    def setEnabled(self, enabled):
        self.enabled.append(enabled)


class _LaserManager:
    def __init__(self, laser):
        self.laser = laser

    def execOn(self, name, func):
        assert name == 'LASER'
        return func(self.laser)


class _Master:
    def __init__(self, detectorManager, laser):
        self.detectorsManager = detectorManager
        self.lasersManager = _LaserManager(laser)


class _Comm:
    def __init__(self):
        self.sigUpdateImage = _Signal()


class _Logger:
    def error(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _controller(fail=False):
    manager = _DetectorManager(fail=fail)
    laser = _Laser()
    ctrl = EventTriggeredControllerBase.__new__(
        EventTriggeredControllerBase
    )
    ctrl._master = _Master(manager, laser)
    ctrl._commChannel = _Comm()
    ctrl._widget = _Widget()
    ctrl._state = EventTriggeredSessionState()
    ctrl._logger = _Logger()
    ctrl._binaryMaskHandle = None
    ctrl._binaryMaskGeneration = 0
    ctrl._binaryMaskFrameSlot = None
    ctrl._binary_stack_list = []
    ctrl.BINARY_FRAMES = 2
    ctrl._set_status = lambda *_args, **_kwargs: None
    return ctrl, manager, laser


def test_binary_mask_capture_holds_event_stream_until_stack_complete(
        monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.'
        'EventTriggeredBaseController.QtCore.QTimer.singleShot',
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    ctrl, manager, laser = _controller()
    captured = []
    ctrl.calculateBinaryMask = captured.append

    ctrl.initiateBinaryMask()

    assert manager.acquired == [(('CAM',), LeasePurpose.EVENT_STREAM)]
    assert laser.enabled[-1] is True
    assert ctrl._state.binaryMaskSignalConnected is True
    assert len(scheduled) == 1

    ctrl.addImgBinStack('CAM', np.ones((2, 2)), False, 1, True)
    ctrl.addImgBinStack('CAM', np.ones((2, 2)) * 2, False, 1, True)

    assert manager.released == ['mask-handle']
    assert laser.enabled[-1] is False
    assert ctrl._state.binaryMaskSignalConnected is False
    assert captured[0].shape == (2, 2, 2)


def test_binary_mask_timeout_releases_lease_and_disables_laser(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.'
        'EventTriggeredBaseController.QtCore.QTimer.singleShot',
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    ctrl, manager, laser = _controller()

    ctrl.initiateBinaryMask()
    scheduled[0][1]()

    assert manager.released == ['mask-handle']
    assert laser.enabled[-1] is False
    assert ctrl._state.binaryMaskSignalConnected is False


def test_binary_mask_acquire_failure_leaves_laser_and_signal_safe(monkeypatch):
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.'
        'EventTriggeredBaseController.QtCore.QTimer.singleShot',
        lambda *_args: None,
    )
    ctrl, manager, laser = _controller(fail=True)

    ctrl.initiateBinaryMask()

    assert manager.released == []
    assert not ctrl._commChannel.sigUpdateImage.slots
    assert ctrl._state.binaryMaskSignalConnected is False
    assert not laser.enabled or laser.enabled[-1] is False


def test_queued_frame_from_previous_mask_capture_cannot_contaminate_restart(
        monkeypatch):
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.'
        'EventTriggeredBaseController.QtCore.QTimer.singleShot',
        lambda *_args: None,
    )
    ctrl, manager, _laser = _controller()

    ctrl.initiateBinaryMask()
    oldQueuedSlot = ctrl._commChannel.sigUpdateImage.slots[0]
    ctrl._cleanupBinaryMaskRecording()

    ctrl.initiateBinaryMask()
    currentSlot = ctrl._commChannel.sigUpdateImage.slots[0]

    # This callable represents a Qt delivery queued before the old signal
    # connection was disconnected. It must not enter the new capture's stack.
    oldQueuedSlot('CAM', np.full((2, 2), 99), False, 1, True)
    assert ctrl._binary_stack_list == []

    currentSlot('CAM', np.ones((2, 2)), False, 1, True)
    assert len(ctrl._binary_stack_list) == 1
    np.testing.assert_array_equal(
        ctrl._binary_stack_list[0], np.ones((2, 2))
    )
    assert len(manager.acquired) == 2

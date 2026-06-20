import importlib
from types import SimpleNamespace

import pytest
from qtpy import QtCore


setup_status_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.SetupStatusController'
)
SetupStatusController = setup_status_module.SetupStatusController


pytestmark = pytest.mark.nohardware


class _Signal:
    def __init__(self):
        self.connected = []
        self.emitted = []

    def connect(self, slot):
        self.connected.append(slot)

    def emit(self, *args):
        self.emitted.append(args)


class _UiElement:
    def __init__(self, value=0):
        self.enabled = None
        self.text = None
        self._value = value
        self.editingFinished = _Signal()
        self.clicked = _Signal()

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setText(self, text):
        self.text = text

    def value(self):
        return self._value


class _SetupStatusWidget:
    def __init__(self):
        self.enabled = None
        self.sigKeyReleased = _Signal()

        self.illuminationStatusLabel = _UiElement()
        self.detectionStatusLabel = _UiElement()

        self.rotationStageHeader = _UiElement()
        self.rotationStagePosLabel = _UiElement()
        self.rotationStagePosEdit = _UiElement(12.5)
        self.jogStepSizeLabel = _UiElement()
        self.jogStepSizeEdit = _UiElement(1.0)
        self.jogPositiveButton = _UiElement()
        self.jogNegativeButton = _UiElement()
        self.currentPosOfRotationStageLabel = _UiElement()
        self.currentPosOfRotationStageDisp = _UiElement()

    def setEnabled(self, enabled):
        self.enabled = enabled


class _CommChannel:
    def __init__(self):
        self.sigSetConfig = _Signal()
        self.sigSetVisibleLayers = _Signal()


class _KeyEvent:
    def __init__(self, key):
        self._key = key

    def isAutoRepeat(self):
        return False

    def key(self):
        return self._key


def test_setup_status_controller_tolerates_missing_optional_rs232_devices(monkeypatch):
    monkeypatch.setattr(setup_status_module, '_APT_AVAILABLE', False)

    widget = _SetupStatusWidget()
    controller = SetupStatusController(
        setupInfo=SimpleNamespace(),
        commChannel=_CommChannel(),
        master=SimpleNamespace(rs232sManager={}),
        widget=widget,
        factory=SimpleNamespace(),
        moduleCommChannel=SimpleNamespace(),
    )

    assert controller._elliptecSliderManager is None
    assert controller._rotationStageManager is None
    assert controller.timer is None
    assert widget.rotationStagePosEdit.enabled is False
    assert widget.jogPositiveButton.enabled is False
    assert widget.currentPosOfRotationStageDisp.text == 'Unavailable'

    controller.keyReleased(_KeyEvent(QtCore.Qt.Key_1))
    controller.setRotationJogStepSizeFromEdit()
    controller.setRotationStagePosFromEdit()
    controller.setRotationStagePos(45)
    controller.getPositions()
    controller.closeEvent()

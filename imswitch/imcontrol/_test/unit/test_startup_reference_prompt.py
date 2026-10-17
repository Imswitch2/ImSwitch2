import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.ImConMainController import ImConMainController

main_controller_module = importlib.import_module(
    'imswitch.imcontrol.controller.ImConMainController'
)

pytestmark = pytest.mark.nohardware


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self, value):
        for callback in list(self.callbacks):
            callback(value)


def _controller(positionerController, *, visible=False):
    controller = ImConMainController.__new__(ImConMainController)
    signal = _Signal()
    view = SimpleNamespace(
        sigModuleVisibilityChanged=signal,
        isVisible=lambda: visible,
    )
    controller.controllers = {'Positioner': positionerController}
    controller._ImConMainController__mainView = view
    return controller, signal


def test_startup_reference_prompt_waits_for_first_visible_event(monkeypatch):
    positionerController = MagicMock()
    positionerController.hasUnreferencedReferenceAxes.return_value = True
    controller, signal = _controller(positionerController, visible=False)

    monkeypatch.setattr(
        main_controller_module.QtCore.QTimer,
        'singleShot',
        lambda _delay, callback: callback(),
    )

    controller._armStartupReferenceDialog()
    assert len(signal.callbacks) == 1
    positionerController.openStartupReferenceDialogIfNeeded.assert_not_called()

    signal.emit(False)
    positionerController.openStartupReferenceDialogIfNeeded.assert_not_called()

    signal.emit(True)
    positionerController.openStartupReferenceDialogIfNeeded.assert_called_once_with()
    assert signal.callbacks == []

    # The one-shot visibility hook was disconnected, so later tab switches do
    # not reopen the startup prompt.
    signal.emit(True)
    positionerController.openStartupReferenceDialogIfNeeded.assert_called_once_with()


def test_startup_reference_prompt_not_armed_without_unreferenced_axes():
    positionerController = MagicMock()
    positionerController.hasUnreferencedReferenceAxes.return_value = False
    controller, signal = _controller(positionerController, visible=False)

    controller._armStartupReferenceDialog()

    assert signal.callbacks == []
    positionerController.openStartupReferenceDialogIfNeeded.assert_not_called()

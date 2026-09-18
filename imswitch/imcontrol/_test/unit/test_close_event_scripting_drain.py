"""imcontrol fails closed when the scripting drain did not finish (A-08)."""
from unittest.mock import Mock, patch

import pytest

from imswitch.imcommon.model import shutdownState
from imswitch.imcontrol.controller.ImConMainController import ImConMainController


def _close_ready_controller():
    controller = ImConMainController.__new__(ImConMainController)
    controller._ImConMainController__logger = Mock()
    controller._ImConMainController__mainView = Mock()
    controller._ImConMainController__factory = Mock()
    controller._ImConMainController__factory.closeAllCreatedControllers.return_value = True
    controller._ImConMainController__masterController = Mock()
    controller._ImConMainController__masterController.closeEvent.return_value = True
    return controller


@pytest.fixture(autouse=True)
def _clean_state():
    shutdownState.reset()
    yield
    shutdownState.reset()


def _close(controller):
    with patch.object(controller, '_shouldSaveWidgetStateOnClose', return_value=False):
        return controller.closeEvent()


def test_managers_are_not_finalized_when_a_script_did_not_drain():
    controller = _close_ready_controller()
    shutdownState.recordScriptingDrain(False, 'The script x.py did not stop within 10 s')
    assert _close(controller) is False
    controller._ImConMainController__masterController.closeEvent.assert_not_called()
    controller._ImConMainController__logger.error.assert_called()


def test_managers_are_finalized_when_scripting_drained_or_did_not_take_part():
    for outcome in (True, None):
        controller = _close_ready_controller()
        shutdownState.reset()
        if outcome is not None:
            shutdownState.recordScriptingDrain(outcome)
        assert _close(controller) is True
        controller._ImConMainController__masterController.closeEvent.assert_called_once()

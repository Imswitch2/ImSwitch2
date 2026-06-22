from unittest.mock import Mock, patch

from qtpy import QtWidgets

from imswitch.imcontrol.controller.ImConMainController import ImConMainController


def _close_ready_controller():
    controller = ImConMainController.__new__(ImConMainController)
    controller._ImConMainController__logger = Mock()
    controller._ImConMainController__mainView = Mock()
    controller._ImConMainController__factory = Mock()
    controller._ImConMainController__masterController = Mock()
    return controller


def test_close_state_prompt_defaults_to_yes():
    controller = _close_ready_controller()

    with patch.object(
        QtWidgets.QMessageBox,
        'question',
        return_value=QtWidgets.QMessageBox.Yes,
    ) as question:
        assert controller._shouldSaveWidgetStateOnClose() is True

    question.assert_called_once()
    assert question.call_args.args[4] == QtWidgets.QMessageBox.Yes


def test_close_event_saves_default_state_when_prompt_accepts():
    controller = _close_ready_controller()
    persistence = Mock()

    with patch.object(controller, '_shouldSaveWidgetStateOnClose', return_value=True), \
            patch(
                'imswitch.imcontrol.controller.ImConMainController.getWidgetStatePersistence',
                return_value=persistence,
            ):
        controller.closeEvent()

    persistence.saveAllWidgetStates.assert_called_once_with('default')
    controller._ImConMainController__factory.closeAllCreatedControllers.assert_called_once()
    controller._ImConMainController__masterController.closeEvent.assert_called_once()


def test_close_event_skips_state_save_when_prompt_declines():
    controller = _close_ready_controller()
    persistence = Mock()

    with patch.object(controller, '_shouldSaveWidgetStateOnClose', return_value=False), \
            patch(
                'imswitch.imcontrol.controller.ImConMainController.getWidgetStatePersistence',
                return_value=persistence,
            ):
        controller.closeEvent()

    persistence.saveAllWidgetStates.assert_not_called()
    controller._ImConMainController__factory.closeAllCreatedControllers.assert_called_once()
    controller._ImConMainController__masterController.closeEvent.assert_called_once()

import weakref
from unittest.mock import Mock, patch

from qtpy import QtWidgets

from imswitch.imcommon.controller.basecontrollers import WidgetControllerFactory
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


def test_restore_warnings_are_shown_to_the_operator():
    controller = _close_ready_controller()

    with patch.object(QtWidgets.QMessageBox, 'warning') as warning:
        controller._showWidgetStateRestoreWarnings(
            'Some settings could not be restored',
            ['Settings: Could not restore Trigger source for Camera1'],
        )

    warning.assert_called_once()
    assert 'Trigger source' in warning.call_args.args[2]


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


def test_close_event_does_not_finalize_hardware_with_live_controller_worker():
    controller = _close_ready_controller()
    controller._ImConMainController__factory.closeAllCreatedControllers.return_value = False

    with patch.object(
        controller, '_shouldSaveWidgetStateOnClose', return_value=False
    ):
        assert controller.closeEvent() is False

    controller._ImConMainController__factory.closeAllCreatedControllers.assert_called_once_with(
        waitTimeoutS=30.0
    )
    controller._ImConMainController__masterController.closeEvent.assert_not_called()


def test_close_event_server_timeout_skips_hardware_finalization():
    controller = _close_ready_controller()
    controller._serverWorker = Mock()
    controller._thread = Mock()
    controller._thread.wait.return_value = False
    controller._ImConMainController__factory.closeAllCreatedControllers.return_value = True

    with patch.object(
        controller, '_shouldSaveWidgetStateOnClose', return_value=False
    ):
        assert controller.closeEvent() is False

    controller._serverWorker.stop.assert_called_once_with()
    controller._thread.quit.assert_called_once_with()
    controller._thread.wait.assert_called_once_with(5000)
    # Controller cleanup may still drain independent workers, but hardware
    # finalization is unsafe while the API server thread remains alive.
    controller._ImConMainController__factory.closeAllCreatedControllers.assert_called_once_with(
        waitTimeoutS=30.0
    )
    controller._ImConMainController__masterController.closeEvent.assert_not_called()


def test_close_event_propagates_incomplete_hardware_shutdown():
    controller = _close_ready_controller()
    controller._ImConMainController__factory.closeAllCreatedControllers.return_value = True
    controller._ImConMainController__masterController.closeEvent.return_value = False

    with patch.object(
        controller, '_shouldSaveWidgetStateOnClose', return_value=False
    ):
        assert controller.closeEvent() is False

    controller._ImConMainController__logger.error.assert_called()


def test_factory_waits_for_shutdown_checker_even_when_close_returns_none():
    owned = Mock()
    owned.closeEvent.return_value = None
    owned.shutdownComplete.side_effect = [False, True]
    factory = WidgetControllerFactory.__new__(WidgetControllerFactory)
    factory._WidgetControllerFactory__createdControllers = [
        weakref.ref(owned)
    ]
    factory._WidgetControllerFactory__logger = Mock()

    with patch(
        'imswitch.imcommon.controller.basecontrollers.'
        'FrameworkUtils.processPendingEventsCurrThread'
    ) as process_events:
        assert factory.closeAllCreatedControllers(waitTimeoutS=0.2) is True

    process_events.assert_called()

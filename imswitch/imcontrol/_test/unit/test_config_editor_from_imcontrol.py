"""Opening the Config Studio from imcontrol, and the restart it may lead to.

Hardware configuration is read once, at startup, so editing it from inside a
running ImSwitch is only half a feature: the other half is noticing that what
was edited is what this session is running, and offering to come back up on it.
"""

from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PyQt5")

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import ostools
from imswitch.imcontrol.controller.ImConMainController import ImConMainController


@pytest.fixture(autouse=True)
def _no_restart_left_armed():
    """A restart request is process-wide; never let one outlive its test."""
    ostools.cancelRestart()
    yield
    ostools.cancelRestart()


def _controller(activeSetupPath='/setups/current.json'):
    controller = ImConMainController.__new__(ImConMainController)
    controller._ImConMainController__logger = Mock()
    controller._ImConMainController__mainView = Mock()
    controller._ImConMainController__configEditor = None
    controller._ImConMainController__activeSetupPath = activeSetupPath
    return controller


def _editor(*, saved=(), activeChanged=False):
    editor = Mock()
    editor.saved_files.return_value = frozenset(saved)
    editor.active_config_changed = activeChanged
    return editor


def _closeEditor(controller, editor):
    """Run the close handler, returning whether a restart prompt was queued."""
    controller._ImConMainController__configEditor = editor
    with patch.object(QtCore.QTimer, 'singleShot') as singleShot:
        controller._onConfigEditorClosed()
    editor.deleteLater.assert_called_once()
    assert controller._ImConMainController__configEditor is None
    return singleShot.called


# ── Which edits are worth a prompt ────────────────────────────────────────
def test_no_prompt_when_the_editor_saved_nothing():
    controller = _controller()
    assert _closeEditor(controller, _editor()) is False


def test_prompt_when_the_running_setup_file_was_saved():
    controller = _controller('/setups/current.json')
    editor = _editor(saved=['/setups/current.json'])
    assert _closeEditor(controller, editor) is True


def test_no_prompt_when_some_other_setup_file_was_saved():
    """Editing a setup this session is not running changes nothing about it."""
    controller = _controller('/setups/current.json')
    editor = _editor(saved=['/setups/a_different_scope.json'])
    assert _closeEditor(controller, editor) is False


def test_prompt_when_the_active_config_was_switched():
    """Even with nothing written, the next startup now loads a different file."""
    controller = _controller('/setups/current.json')
    assert _closeEditor(controller, _editor(activeChanged=True)) is True


def test_a_second_close_is_a_no_op():
    controller = _controller()
    controller._ImConMainController__configEditor = None
    with patch.object(QtCore.QTimer, 'singleShot') as singleShot:
        controller._onConfigEditorClosed()
    assert not singleShot.called


def test_an_unresolvable_active_setup_never_prompts_on_a_save():
    """No active path to compare against must not become 'everything matches'."""
    controller = _controller(None)
    editor = _editor(saved=['/setups/current.json'])
    assert _closeEditor(controller, editor) is False


# ── Declining the prompt ──────────────────────────────────────────────────
def test_declining_the_prompt_leaves_imswitch_running():
    controller = _controller()
    with patch(
        'imswitch.imcontrol.controller.ImConMainController.guitools.askYesNoQuestion',
        return_value=False,
    ):
        controller._promptRestartAfterConfigEdit()

    assert ostools.restartRequested() is None
    controller._ImConMainController__mainView.window.assert_not_called()


def test_accepting_the_prompt_restarts():
    controller = _controller()
    with patch(
        'imswitch.imcontrol.controller.ImConMainController.guitools.askYesNoQuestion',
        return_value=True,
    ):
        controller._promptRestartAfterConfigEdit()

    assert ostools.restartRequested() == 'imswitch'
    controller._ImConMainController__mainView.window.return_value.close.assert_called_once()


# ── The restart itself ────────────────────────────────────────────────────
def test_restart_goes_through_shutdown_rather_than_execing_on_the_spot():
    """The whole point: managers get finalized before the process is replaced."""
    controller = _controller()
    with patch.object(ostools, 'restartSoftware') as restartSoftware:
        controller._restartAfterShutdown()

    restartSoftware.assert_not_called()
    assert ostools.restartRequested() == 'imswitch'
    controller._ImConMainController__mainView.window.return_value.close.assert_called_once()


def test_a_vetoed_close_disarms_the_restart():
    """A close the user backs out of must not restart at the next close."""
    controller = _controller()
    controller._ImConMainController__mainView.window.return_value.close.return_value = False

    controller._restartAfterShutdown()

    assert ostools.restartRequested() is None


# ── One window at a time ──────────────────────────────────────────────────
def test_reopening_raises_the_window_already_open():
    controller = _controller()
    editor = Mock()
    controller._ImConMainController__configEditor = editor

    controller.openConfigEditor()

    editor.raise_.assert_called_once()
    editor.activateWindow.assert_called_once()


def test_a_failure_to_open_is_reported_and_leaves_no_half_open_window():
    controller = _controller()
    with patch(
        'imswitch.imcontrol.view.configeditor.MainWindow',
        side_effect=RuntimeError('no templates'),
    ), patch.object(QtWidgets.QMessageBox, 'critical') as critical:
        controller.openConfigEditor()

    critical.assert_called_once()
    assert 'no templates' in critical.call_args.args[2]
    assert controller._ImConMainController__configEditor is None

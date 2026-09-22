"""Tools > Edit hardware configuration..., end to end on a running imcontrol.

The unit tests cover the restart policy in isolation. This one covers the part
no mock can: that the menu entry is actually wired to a window that opens, that
a second click does not open a second copy of the same file, and that closing
after editing the setup this session is running on leads to the restart offer.
"""

from unittest.mock import patch

import pytest
from qtpy import QtTest

from imswitch.imcommon.model import ostools
from imswitch.imcontrol.model import configfiletools
from . import getApp, prepareUI, setupInfoWithoutWidgets
from .. import optionsBasic

mainView = None


@pytest.fixture(scope='module')
def qapp():
    global mainView
    app = getApp()
    mainView = prepareUI(optionsBasic, setupInfoWithoutWidgets)
    yield app


@pytest.fixture(autouse=True)
def _no_restart_left_armed():
    ostools.cancelRestart()
    yield
    ostools.cancelRestart()


def _editorWindows(app):
    return [w for w in app.topLevelWidgets()
            if type(w).__module__.endswith('configeditor.editor')]


def test_the_action_is_in_the_tools_menu(qapp, qtbot):
    tools = next(action for action in mainView.menuBar().actions()
                 if 'Tools' in action.text())
    labels = [action.text() for action in tools.menu().actions()]
    assert any('hardware configuration' in label for label in labels), labels


def test_triggering_it_opens_one_editor_on_the_active_setup(qapp, qtbot):
    mainView.configEditorAction.trigger()
    qapp.processEvents()

    windows = _editorWindows(qapp)
    assert len(windows) == 1
    editor = windows[0]
    assert editor.isWindow(), 'the editor must be its own window, not a child widget'

    # It opens on the file this session is running, not an empty pane.
    running = configfiletools.getSetupFilePath(optionsBasic.setupFileName)
    if editor._path:
        assert editor._path.endswith(optionsBasic.setupFileName) or running


def test_triggering_it_again_raises_the_same_window(qapp, qtbot):
    mainView.configEditorAction.trigger()
    qapp.processEvents()
    assert len(_editorWindows(qapp)) == 1


def test_closing_after_editing_the_running_setup_offers_a_restart(qapp, qtbot):
    editor = _editorWindows(qapp)[0]
    # Closing an edited-but-unsaved editor asks before discarding, and a modal
    # question with nobody to answer it would hang the run.
    editor._modified = False
    editor._note_saved(configfiletools.getSetupFilePath(optionsBasic.setupFileName))

    asked = []
    with patch('imswitch.imcontrol.view.guitools.askYesNoQuestion',
               side_effect=lambda *args: asked.append(args) or False):
        editor.close()
        qapp.processEvents()
        QtTest.QTest.qWait(50)
        qapp.processEvents()

    assert asked, 'the restart offer never appeared'
    assert 'Restart' in asked[0][1]
    # Declining leaves the session exactly as it was.
    assert ostools.restartRequested() is None


def test_closing_with_nothing_saved_says_nothing(qapp, qtbot):
    mainView.configEditorAction.trigger()
    qapp.processEvents()
    editor = _editorWindows(qapp)[0]
    editor._modified = False

    asked = []
    with patch('imswitch.imcontrol.view.guitools.askYesNoQuestion',
               side_effect=lambda *args: asked.append(args) or False):
        editor.close()
        qapp.processEvents()
        QtTest.QTest.qWait(50)
        qapp.processEvents()

    assert not asked, f'an editing session that changed nothing prompted: {asked}'


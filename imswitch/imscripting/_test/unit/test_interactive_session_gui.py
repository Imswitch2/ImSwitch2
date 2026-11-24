"""The interactive session through the real ImScripting window: Run cell
(Ctrl+Enter) and Run selection (F9) leave their variables in the session,
the console and the variable inspector see them, both are disabled while a
script runs, Reset session is too, and Run all stays fresh."""
import pytest
from qtpy import QtCore

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imscripting.controller.basecontrollers import ImScrWidgetControllerFactory
from imswitch.imscripting.controller.CommunicationChannel import CommunicationChannel
from imswitch.imscripting.controller.ImScrMainViewController import ImScrMainViewController
from imswitch.imscripting.model import getActionsScope
from imswitch.imscripting.view.ImScrMainView import ImScrMainView


@pytest.fixture
def window(qtbot):
    channel = ModuleCommunicationChannel()
    scope = {'moduleCommChannel': channel}
    scope.update(getActionsScope(scope.copy()))
    view = ImScrMainView()
    qtbot.addWidget(view)
    factory = ImScrWidgetControllerFactory(scope, CommunicationChannel(), channel)
    controller = factory.createController(ImScrMainViewController, view)
    executor = controller.editorController.scriptExecutor
    yield view, controller, executor
    executor.shutdown(timeoutMs=4000)


def _editor(view):
    return view.editor.getCurrentInstance()


def _finish(qtbot, executor):
    qtbot.waitUntil(lambda: not executor.isBusy(), timeout=5000)


def _key(qtbot, editor, key, modifier=QtCore.Qt.NoModifier):
    qtbot.keyClick(editor.scintilla, key, modifier)


def test_ctrl_enter_runs_the_cell_into_the_session(qtbot, window):
    view, _controller, executor = window
    editor = _editor(view)
    editor.setText('# %% first\nx = 41\n# %% second\ny = x + 1\n')
    editor.setCursorLine(1)

    _key(qtbot, editor, QtCore.Qt.Key_Return, QtCore.Qt.ControlModifier)
    _finish(qtbot, executor)
    assert executor.session.namespace['x'] == 41
    assert 'y' not in executor.session.namespace           # only the cell ran
    assert editor.getText().count('\n') == 4                 # no line break typed

    assert editor.cursorLine() == 1                          # Ctrl+Enter stays

    _key(qtbot, editor, QtCore.Qt.Key_Return, QtCore.Qt.ShiftModifier)
    _finish(qtbot, executor)
    assert editor.cursorLine() == 2                          # Shift+Enter: next cell

    _key(qtbot, editor, QtCore.Qt.Key_Return, QtCore.Qt.ShiftModifier)
    _finish(qtbot, executor)
    assert executor.session.namespace['y'] == 42             # x from the first cell
    assert editor.cursorLine() == 2                          # the last cell: stays


def test_f9_runs_the_current_line_and_the_console_sees_it(qtbot, window):
    view, _controller, executor = window
    editor = _editor(view)
    editor.setText('a = 1\nb = a * 10\n')
    editor.setCursorLine(1)
    executor.session.namespace['a'] = 5                      # as if a console had set it

    _key(qtbot, editor, QtCore.Qt.Key_F9)
    _finish(qtbot, executor)

    assert executor.session.namespace['b'] == 50
    assert view.console.localNamespace is executor.session.namespace


def test_the_inspector_lists_variables_and_follows_the_console(qtbot, window):
    view, _controller, executor = window
    editor = _editor(view)
    editor.setText('import numpy as np\nframes = np.zeros((3, 4, 5), dtype=np.uint16)\n')
    editor.sigRunCellClicked.emit(False)
    _finish(qtbot, executor)
    qtbot.waitUntil(lambda: view.variables.table.rowCount() == 1, timeout=2000)
    assert [view.variables.table.item(0, c).text() for c in range(5)] == [
        'frames', 'ndarray', '3 x 4 x 5', 'uint16', '120 B']

    view.console.localNamespace['mean'] = 2.5                # what a console command does
    view.console.sigCommandExecuted.emit()
    assert view.variables.table.rowCount() == 2


def test_console_and_reset_are_disabled_while_a_script_runs(qtbot, window, monkeypatch):
    view, controller, executor = window
    warned = []
    monkeypatch.setattr('imswitch.imscripting.view.guitools.showWarning',
                        lambda _w, _t, message, **_k: warned.append(message))
    editor = _editor(view)
    editor.setText('import time\nkept = 1\ntime.sleep(0.4)\n')
    editor.sigRunCellClicked.emit(False)

    assert not view.console.input.isEnabled()
    assert not editor.resetSessionButton.isEnabled()
    assert not view.variables.table.isEnabled()
    assert controller.editorController.resetSession() is False
    assert warned and 'running' in warned[0]
    _finish(qtbot, executor)
    qtbot.waitUntil(lambda: view.console.input.isEnabled(), timeout=2000)
    assert editor.resetSessionButton.isEnabled()
    assert executor.session.namespace['kept'] == 1


def test_reset_session_forgets_and_run_all_stays_fresh(qtbot, window, monkeypatch):
    view, controller, executor = window
    editor = _editor(view)
    editor.setText('z = 3\n')
    editor.sigRunCellClicked.emit(False)
    _finish(qtbot, executor)

    asked = []
    monkeypatch.setattr('imswitch.imscripting.view.guitools.askYesNoQuestion',
                        lambda _w, _t, question, **_k: asked.append(question) or True)
    assert controller.editorController.resetSession() is not False
    assert 'z' not in executor.session.namespace
    assert asked and 'z' in asked[0]                          # names what it forgets
    assert view.variables.table.rowCount() == 0
    assert 'Session reset' in view.console.output.toPlainText()

    editor.setText('w = 1\n')
    editor.sigRunAllClicked.emit(editor.getText())
    _finish(qtbot, executor)
    assert 'w' not in executor.session.namespace             # Run all: fresh scope

"""The Script Runner panel: a form built from a script's scriptInput() calls,
and runs of that script through the Scripting module."""
import importlib
import time
from types import SimpleNamespace

import pytest
from qtpy import QtWidgets

import imswitch.imscripting as imscriptingPackage
from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcontrol.controller.controllers.ScriptRunnerController import (
    ScriptRunnerController,
)
from imswitch.imcontrol.view.widgets.ScriptRunnerWidget import ScriptRunnerWidget
from imswitch.imscripting.controller.EditorController import EditorController
from imswitch.imscripting.model import ScriptExecutor

# By import_module: the controllers package hands out the class under this name.
controllerModule = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.ScriptRunnerController'
)

_APP = None

SCRIPT = (
    "exposure = scriptInput('exposure_ms', 10.0, 'Exposure per frame')\n"
    "frames = scriptInput('frames', 3)\n"
    "label = scriptInput('label', 'run')\n"
    "print('got', repr(exposure), repr(frames), repr(label))\n"
)


@pytest.fixture(scope='module')
def qapp():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _waitFor(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'timed out'
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture
def stateFile(tmp_path, monkeypatch):
    path = tmp_path / 'config' / 'script_runner.json'
    monkeypatch.setattr(controllerModule, '_stateFilePath', str(path))
    return path


@pytest.fixture
def channel(qapp):
    channel = ModuleCommunicationChannel()
    channel.register(imscriptingPackage)
    return channel


@pytest.fixture
def executor(qapp, channel):
    """ The Scripting module's side: an EditorController without its view,
    wired to the channel like its __init__ does. """
    executor = ScriptExecutor(scriptScope={})
    editor = EditorController.__new__(EditorController)
    editor._EditorController__logger = SimpleNamespace(warning=lambda *_: None)
    editor._moduleCommChannel = channel
    editor._commChannel = SimpleNamespace(sigExecutionStarted=SimpleNamespace(emit=lambda: None))
    editor._widget = SimpleNamespace(setStopping=lambda *_: None)
    editor.scriptExecutor = executor
    executor.sigExecutionStarted.connect(channel.sigScriptRunStarted)
    executor.sigExecutionFinished.connect(lambda r: editor._onExecutionFinished(r))
    channel.sigRunScriptFile.connect(lambda *args: editor.runScriptFile(*args))
    channel.sigStopScriptRun.connect(lambda requestId: editor.stopScriptRun(requestId))
    yield executor
    executor.shutdown(timeoutMs=3000)


def _panel(channel):
    widget = ScriptRunnerWidget(None)
    controller = ScriptRunnerController(
        None, None, None, widget=widget, factory=None, moduleCommChannel=channel,
    )
    return controller, widget


def _script(tmp_path, text=SCRIPT, name='measure.py'):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return str(path)


def _labels(widget):
    layout = widget.inputsLayout
    return [layout.itemAt(row, QtWidgets.QFormLayout.LabelRole).widget().text()
            for row in range(layout.rowCount())]


def test_the_form_has_a_field_per_input_filled_with_its_default(channel, stateFile, tmp_path):
    controller, widget = _panel(channel)
    assert not widget.runButton.isEnabled()  # no script yet

    controller.setScriptPath(_script(tmp_path))

    assert widget.scriptLabel.text() == 'measure.py'
    assert _labels(widget) == ['exposure_ms', 'frames', 'label']
    assert widget.getInputTexts() == {'exposure_ms': '10.0', 'frames': '3', 'label': 'run'}
    assert widget.runButton.isEnabled()


def test_a_run_passes_the_typed_values_and_reports_the_end(channel, executor, stateFile,
                                                           tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path))
    widget._inputEdits['exposure_ms'].setText('25')
    widget._inputEdits['label'].setText('')  # empty: the script's default
    finished = []
    channel.sigScriptRunFinished.connect(finished.append)

    widget.runButton.click()
    assert not widget.runButton.isEnabled() and widget.stopButton.isEnabled()
    _waitFor(lambda: finished)

    (result,) = finished
    assert "got 25.0 3 'run'" in result.stdout
    assert result.inputs == {'exposure_ms': 25.0, 'frames': 3}
    assert widget.statusLabel.text().startswith('Finished in')
    assert widget.runButton.isEnabled() and not widget.stopButton.isEnabled()


def test_a_value_that_does_not_fit_is_marked_and_nothing_runs(channel, stateFile, tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path))
    requests = []
    channel.sigRunScriptFile.connect(lambda *args: requests.append(args))

    widget._inputEdits['frames'].setText('2.5')
    widget._inputEdits['frames'].textEdited.emit('2.5')
    assert 'Expected a whole number' in widget._inputEdits['frames'].toolTip()

    widget.runButton.click()
    assert requests == []
    assert 'frames' in widget.statusLabel.text()


def test_a_failing_script_shows_its_last_error_line(channel, executor, stateFile, tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path, "raise ValueError('stage not homed')\n"))
    finished = []
    channel.sigScriptRunFinished.connect(finished.append)

    widget.runButton.click()
    _waitFor(lambda: finished)

    assert widget.statusLabel.text() == 'Failed: ValueError: stage not homed'
    assert 'Traceback' in widget.statusLabel.toolTip()


def test_stop_cancels_the_run(channel, executor, stateFile, tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path, 'sleep(30)\n'))
    finished = []
    channel.sigScriptRunFinished.connect(finished.append)

    widget.runButton.click()
    _waitFor(executor.isExecuting)
    widget.stopButton.click()
    _waitFor(lambda: finished)

    assert widget.statusLabel.text() == 'Stopped'


def test_run_is_disabled_while_another_script_runs(channel, executor, stateFile, tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path))

    other = executor.execute(None, 'sleep(30)\n')  # e.g. from the editor
    assert not widget.runButton.isEnabled()
    assert widget.runButton.toolTip() == 'Another script is running.'

    _waitFor(executor.isExecuting)
    executor.cancel()
    _waitFor(widget.runButton.isEnabled)
    assert other.status.value == 'cancelled'


def test_run_is_disabled_without_the_scripting_module(qapp, stateFile, tmp_path):
    controller, widget = _panel(ModuleCommunicationChannel())
    controller.setScriptPath(_script(tmp_path))
    assert not widget.runButton.isEnabled()
    assert 'Scripting module' in widget.runButton.toolTip()


def test_a_syntax_error_is_shown_and_blocks_the_run(channel, stateFile, tmp_path):
    controller, widget = _panel(channel)
    controller.setScriptPath(_script(tmp_path, 'x = scriptInput(\n'))
    assert widget.messageLabel.text().startswith('Syntax error')
    assert not widget.runButton.isEnabled()


def test_the_form_follows_the_file_but_keeps_typed_values(channel, stateFile, tmp_path):
    controller, widget = _panel(channel)
    path = _script(tmp_path)
    controller.setScriptPath(path)
    widget._inputEdits['frames'].setText('8')
    widget._inputEdits['frames'].textEdited.emit('8')

    _script(tmp_path, "a = scriptInput('frames', 3)\nb = scriptInput('exposure_ms', 99.0)\n"
                      "c = scriptInput('binning', 2)\n")
    controller._onFileChanged(path)

    assert _labels(widget) == ['frames', 'exposure_ms', 'binning']
    assert widget.getInputTexts() == {'frames': '8', 'exposure_ms': '99.0', 'binning': '2'}


def test_the_script_and_typed_values_are_remembered(channel, stateFile, tmp_path):
    controller, widget = _panel(channel)
    path = _script(tmp_path)
    controller.setScriptPath(path)
    widget._inputEdits['label'].setText('kept')
    widget._inputEdits['label'].textEdited.emit('kept')
    controller.closeEvent()

    _, restored = _panel(channel)

    assert restored.scriptLabel.toolTip() == path
    assert restored.getInputTexts()['label'] == 'kept'

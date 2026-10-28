"""Runs of a script file with scriptInput() values, as the ScriptRunner widget
in imcontrol asks for them over the module communication channel."""
import time
from types import SimpleNamespace

import pytest
from qtpy import QtWidgets

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imscripting.controller.EditorController import EditorController
from imswitch.imscripting.model import ScriptExecutor, ScriptRunStatus

_APP = None


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
def executor(qapp):
    executor = ScriptExecutor(scriptScope={})
    yield executor
    executor.shutdown(timeoutMs=3000)


def _editor(executor, channel):
    """ An EditorController wired like its __init__ does, without its view. """
    ctrl = EditorController.__new__(EditorController)
    ctrl._EditorController__logger = SimpleNamespace(warning=lambda *_: None)
    ctrl._moduleCommChannel = channel
    ctrl._commChannel = SimpleNamespace(sigExecutionStarted=SimpleNamespace(emit=lambda: None))
    ctrl._widget = SimpleNamespace(setStopping=lambda *_: None)
    ctrl.scriptExecutor = executor
    # Lambdas: Qt does not deliver to bound methods of a QObject whose
    # __init__ never ran.
    executor.sigExecutionStarted.connect(channel.sigScriptRunStarted)
    executor.sigExecutionFinished.connect(lambda r: ctrl._onExecutionFinished(r))
    channel.sigRunScriptFile.connect(lambda *args: ctrl.runScriptFile(*args))
    channel.sigStopScriptRun.connect(lambda requestId: ctrl.stopScriptRun(requestId))
    return ctrl


def _record(channel):
    events = []
    channel.sigScriptRunStarted.connect(lambda r: events.append(('started', r)))
    channel.sigScriptRunFinished.connect(lambda r: events.append(('finished', r)))
    return events


def test_script_input_returns_the_given_value_or_the_default(executor):
    code = (
        "a = scriptInput('a', 1.0)\n"
        "b = scriptInput('b', 2)\n"
        "c = scriptInput('c', 3)\n"
        "print(repr(a), repr(b), repr(c))\n"
    )
    result = executor.execute(None, code, inputs={'a': 5.5, 'b': '7'}, requestId='req')
    _waitFor(lambda: result.status != ScriptRunStatus.RUNNING)

    assert result.status == ScriptRunStatus.SUCCEEDED, result.error
    # A value given as text is converted to the type of the default.
    assert '5.5 7 3' in result.stdout
    assert result.request_id == 'req' and result.inputs == {'a': 5.5, 'b': '7'}


def test_scripts_imported_by_the_run_see_the_same_inputs(executor, tmp_path):
    # importScript() adds the actions after running the module, so only its
    # functions can call them.
    (tmp_path / 'helper.py').write_text("def value():\n    return scriptInput('a', 0)\n",
                                        encoding='utf-8')
    main = tmp_path / 'main.py'
    result = executor.execute(str(main), "print(importScript('helper.py').value())\n",
                              inputs={'a': 42})
    _waitFor(lambda: result.status != ScriptRunStatus.RUNNING)
    assert result.status == ScriptRunStatus.SUCCEEDED, result.error
    assert '42' in result.stdout


def test_a_file_run_is_announced_and_runs_with_its_path(executor, tmp_path):
    channel = ModuleCommunicationChannel()
    _editor(executor, channel)
    events = _record(channel)
    script = tmp_path / 'run_me.py'
    script.write_text("print(getScriptDirPath(), scriptInput('n', 1))\n", encoding='utf-8')

    channel.sigRunScriptFile.emit(str(script), {'n': 9}, 'req-1')
    _waitFor(lambda: any(kind == 'finished' for kind, _ in events))

    (started, startResult), (finished, result) = events
    assert (started, finished) == ('started', 'finished') and startResult is result
    assert result.status == ScriptRunStatus.SUCCEEDED, result.error
    assert result.request_id == 'req-1' and result.script_path == str(script)
    assert f'{tmp_path} 9' in result.stdout


def test_a_file_run_is_refused_while_another_script_runs(executor, tmp_path):
    channel = ModuleCommunicationChannel()
    _editor(executor, channel)
    running = executor.execute(None, 'sleep(30)\n')
    events = _record(channel)
    script = tmp_path / 'other.py'
    script.write_text('print(1)\n', encoding='utf-8')

    channel.sigRunScriptFile.emit(str(script), {}, 'req-2')

    ((kind, refused),) = events
    assert kind == 'finished' and refused.status == ScriptRunStatus.FAILED
    assert refused.request_id == 'req-2' and 'Another script is running' in refused.error
    assert running.status == ScriptRunStatus.RUNNING  # left alone
    _waitFor(executor.isExecuting)
    executor.cancel()
    _waitFor(lambda: running.status != ScriptRunStatus.RUNNING)


def test_a_missing_file_is_refused(executor, tmp_path):
    channel = ModuleCommunicationChannel()
    _editor(executor, channel)
    events = _record(channel)

    channel.sigRunScriptFile.emit(str(tmp_path / 'gone.py'), {}, 'req-3')

    ((kind, refused),) = events
    assert refused.status == ScriptRunStatus.FAILED and 'Could not read' in refused.error


def test_stop_only_stops_the_run_with_that_request_id(executor, tmp_path):
    channel = ModuleCommunicationChannel()
    _editor(executor, channel)
    events = _record(channel)
    script = tmp_path / 'long.py'
    script.write_text('sleep(30)\n', encoding='utf-8')
    channel.sigRunScriptFile.emit(str(script), {}, 'mine')
    _waitFor(executor.isExecuting)

    channel.sigStopScriptRun.emit('someone-else')
    assert not executor.isStopping()

    channel.sigStopScriptRun.emit('mine')
    _waitFor(lambda: any(kind == 'finished' for kind, _ in events))
    assert events[-1][1].status == ScriptRunStatus.CANCELLED

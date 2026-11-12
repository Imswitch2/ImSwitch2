"""The interactive session: Run all executes in a fresh namespace, Run cell
and Run selection in one that lives on between runs and that the console
shares; Reset forgets it, only while idle; tracebacks name editor lines."""
import numpy as np
import pytest

from imswitch.imscripting.model import (
    ScriptExecutor, ScriptRunStatus, cellAt, cellStarts, describeValue, isShowable,
)
from imswitch.imscripting.model.ScriptSession import ScriptSession, compileForEditor


@pytest.fixture
def executor(qtbot):
    ex = ScriptExecutor(scriptScope={'base': 'installed'}, escalateAfterMs=5000)
    ex.sigOutputAppended.connect(lambda _t: None)
    yield ex
    ex.shutdown(timeoutMs=4000)


def _run(qtbot, executor, code, **kwargs):
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=5000) as blocker:
        executor.execute(None, code, **kwargs)
    return blocker.args[0]


# --- the session -------------------------------------------------------------

def test_session_runs_share_variables_and_run_all_does_not(qtbot, executor):
    assert _run(qtbot, executor, 'frames = [1, 2, 3]', session=True).status == ScriptRunStatus.SUCCEEDED
    result = _run(qtbot, executor, 'total = sum(frames)', session=True)
    assert result.status == ScriptRunStatus.SUCCEEDED and result.session is True
    assert executor.session.namespace['total'] == 6

    fresh = _run(qtbot, executor, 'print(frames)')            # Run all: fresh scope
    assert fresh.status == ScriptRunStatus.FAILED and fresh.session is False
    assert "name 'frames' is not defined" in fresh.error
    fresh = _run(qtbot, executor, 'leaked = 1')
    assert 'leaked' not in executor.session.namespace       # and leaves nothing behind


def test_session_runs_have_the_installed_names_and_helpers(qtbot, executor):
    result = _run(qtbot, executor, 'seen = (base, callable(sleep), callable(show))', session=True)
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert executor.session.namespace['seen'] == ('installed', True, True)


def test_user_variables_leave_out_installed_names_modules_and_functions(qtbot, executor):
    code = 'import math\ndef f():\n    pass\nimage = np_zeros = [[0]]\n_private = 1\nx = 2'
    _run(qtbot, executor, code, session=True)
    assert list(executor.session.userVariables()) == ['image', 'np_zeros', 'x']


def test_reset_forgets_and_reinstalls_and_keeps_the_same_dict(qtbot, executor):
    namespace = executor.session.namespace                    # the console holds this
    _run(qtbot, executor, 'x = 1\nbase = "overwritten"', session=True)
    resets = []
    executor.sigSessionReset.connect(lambda: resets.append(True))

    assert executor.resetSession() is True

    assert executor.session.namespace is namespace
    assert 'x' not in namespace and namespace['base'] == 'installed'
    assert resets == [True]


def test_reset_is_refused_while_a_script_runs(qtbot, executor):
    executor.execute(None, 'import time\nx = 1\ntime.sleep(0.5)', session=True)
    # Refused at once, before the script thread has even picked the run up:
    # the run is going to use the session.
    assert executor.isBusy() is True
    assert executor.resetSession() is False
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=5000):
        pass
    assert executor.session.namespace['x'] == 1
    assert executor.isBusy() is False
    assert executor.resetSession() is True


def test_started_and_finished_bracket_every_run(qtbot, executor):
    events = []
    executor.sigExecutionStarted.connect(lambda r: events.append(('started', r.session)))
    executor.sigExecutionFinished.connect(lambda r: events.append(('finished', r.session)))
    _run(qtbot, executor, 'pass', session=True)
    _run(qtbot, executor, 'pass')
    assert events == [('started', True), ('finished', True),
                      ('started', False), ('finished', False)]


# --- editor lines ------------------------------------------------------------

def test_a_traceback_names_the_editor_line_and_shows_the_editor_text(qtbot, executor):
    code = 'a = 1\nraise ValueError("boom")'
    result = _run(qtbot, executor, code, session=True, firstLine=9)
    assert result.status == ScriptRunStatus.FAILED
    assert 'line 11' in result.error                          # editor lines are 1-based
    assert 'raise ValueError("boom")' in result.error


def test_an_indented_selection_runs():
    namespace = {}
    exec(compileForEditor('    x = 1\n    y = x + 1\n', 4, '<sel>'), namespace)
    assert namespace['y'] == 2


def test_a_full_script_is_not_dedented():
    code = 'text = """\n    kept\n"""\n'
    namespace = {}
    exec(compileForEditor(code, 0, '<all>'), namespace)
    assert namespace['text'] == '\n    kept\n'


# --- cells -------------------------------------------------------------------

TEXT = '\n'.join([
    'import numpy as np',      # 0  cell 0
    '# %% acquire',            # 1  cell 1
    'frames = 1',              # 2
    '',                        # 3
    '#%%',                     # 4  cell 2
    'mean = 2',                # 5
])


@pytest.mark.parametrize('line, expected', [
    (0, (0, 1)), (1, (1, 4)), (2, (1, 4)), (3, (1, 4)), (4, (4, 6)), (5, (4, 6)),
    (99, (4, 6)),
])
def test_cell_at_a_line(line, expected):
    first, end, code = cellAt(TEXT, line)
    assert (first, end) == expected
    assert code == '\n'.join(TEXT.split('\n')[first:end])


def test_cell_starts_and_a_file_without_markers():
    assert cellStarts(TEXT) == [0, 1, 4]
    assert cellAt('a = 1\nb = 2', 1) == (0, 2, 'a = 1\nb = 2')


# --- what the inspector shows ------------------------------------------------

def test_describe_an_array_a_dict_of_arrays_and_a_scalar():
    frames = np.zeros((100, 512, 512), dtype=np.uint16)
    info = describeValue(frames)
    assert (info['type'], info['shape'], info['dtype'], info['size']) == (
        'ndarray', '100 x 512 x 512', 'uint16', '50.0 MB')
    assert info['value'] == '0 to 0'
    both = describeValue({'Camera': frames, 'APD': np.ones((2, 2))})
    assert both['shape'] == '2 entries' and both['value'] == 'Camera, APD'
    assert describeValue(3.5)['value'] == '3.5'
    assert isShowable(frames) and isShowable({'a': frames})
    assert not isShowable(np.zeros(5)) and not isShowable(3.5)


def test_a_session_without_a_run_lists_nothing():
    session = ScriptSession({'api': object()})
    assert session.userVariables() == {}
    session.namespace['x'] = 1
    assert session.userVariables() == {'x': 1}

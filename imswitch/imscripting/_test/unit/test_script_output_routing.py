"""Per-thread output capture (plan D-06 / A-07): a script's prints are
captured for its run; other threads' prints are not swallowed by it."""
import sys

from imswitch.imcommon.model import cancellableSleep
from imswitch.imscripting.model import ScriptExecutor, ScriptRunStatus
from imswitch.imscripting.model.ScriptExecutor import _ThreadRoutingStream


def test_main_thread_prints_are_not_captured_by_a_running_script(qtbot, capsys):
    marks = []
    executor = ScriptExecutor(
        scriptScope={'marks': marks, 'cancellableSleep': cancellableSleep},
        cleanupBudgetS=30.0, escalateAfterMs=200,
    )
    executor.sigOutputAppended.connect(lambda _t: None)
    try:
        result = executor.execute(None, "print('from the script')\nmarks.append('started')\ncancellableSleep(30)")
        qtbot.waitUntil(lambda: 'started' in marks, timeout=3000)
        print('from the main thread')
        sys.stderr.write('main stderr\n')
        executor.cancel()
        with qtbot.waitSignal(executor.sigExecutionFinished, timeout=4000):
            pass
        captured = capsys.readouterr()
        assert 'from the main thread' in captured.out
        assert 'main stderr' in captured.err
        assert 'from the main thread' not in result.stdout
        assert 'from the script' in result.stdout
        assert 'from the script' not in captured.out
        assert result.status == ScriptRunStatus.CANCELLED
    finally:
        executor.shutdown(timeoutMs=3000)


def test_a_foreign_stdout_replacement_is_wrapped_not_clobbered(qtbot):
    import io
    foreign = io.StringIO()
    previous = sys.stdout
    sys.stdout = foreign
    try:
        executor = ScriptExecutor(scriptScope={})
        executor.sigOutputAppended.connect(lambda _t: None)
        result = executor.execute(None, "print('inside')")
        with qtbot.waitSignal(executor.sigExecutionFinished, timeout=4000):
            pass
        executor.shutdown(timeoutMs=2000)
        assert 'inside' in result.stdout
        assert isinstance(sys.stdout, _ThreadRoutingStream)
        print('outside')
        assert 'outside' in foreign.getvalue()      # delegated to the foreign stream
        assert 'inside' not in foreign.getvalue()
    finally:
        sys.stdout = previous

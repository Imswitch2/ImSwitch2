"""Application shutdown phase (plan A-08 / P-9).

``shutdownModules`` runs ``prepareShutdown`` in reverse creation order before
any ``closeEvent``; imscripting's ``prepareShutdown`` closes the API gate and
drains the script thread with a bound, recording the outcome for imcontrol.
"""
import threading

import pytest
from qtpy import QtCore

from imswitch.imcommon.applaunch import shutdownModules
from imswitch.imcommon.model import (
    APIExport, apiGate, cancellableSleep, generateAPI, shutdownState,
)
from imswitch.imscripting.controller.ImScrMainController import ImScrMainController
from imswitch.imscripting.model import ScriptExecutor, ScriptRunStatus


class _Module:
    def __init__(self, name, log, *, prepare=True, fail=False):
        self.name = name
        self.log = log
        self.fail = fail
        if prepare:
            self.prepareShutdown = self._prepare

    def _prepare(self):
        self.log.append(('prepare', self.name))
        if self.fail:
            raise RuntimeError('boom')

    def closeEvent(self):
        self.log.append(('close', self.name))
        if self.fail:
            raise RuntimeError('boom')


def test_prepare_runs_in_reverse_order_before_any_close():
    log = []
    modules = [_Module('imcontrol', log), _Module('improcess', log, prepare=False),
               _Module('imscripting', log)]
    shutdownModules(modules)
    assert log == [
        ('prepare', 'imscripting'), ('prepare', 'imcontrol'),
        ('close', 'imcontrol'), ('close', 'improcess'), ('close', 'imscripting'),
    ]


def test_a_failing_module_does_not_stop_the_others():
    log = []
    modules = [_Module('a', log, fail=True), _Module('b', log)]
    shutdownModules(modules)
    assert log == [('prepare', 'b'), ('prepare', 'a'), ('close', 'a'), ('close', 'b')]


class _Target(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.calls = []

    @APIExport(runOnUIThread=True)
    def poke(self) -> None:
        self.calls.append(threading.get_ident())


@pytest.fixture
def scripting(qtbot):
    apiGate.reopen()
    shutdownState.reset()
    target = _Target()
    api = generateAPI([target])
    marks = []
    executor = ScriptExecutor(
        scriptScope={'api': api, 'marks': marks, 'cancellableSleep': cancellableSleep},
        cleanupBudgetS=30.0, escalateAfterMs=200,
    )
    executor.sigOutputAppended.connect(lambda _t: None)
    controller = ImScrMainController.__new__(ImScrMainController)

    class _Editor:
        scriptExecutor = executor

    class _MainView:
        editorController = _Editor()

    controller.mainViewController = _MainView()
    yield controller, executor, target, marks
    executor.shutdown(timeoutMs=4000)
    apiGate.reopen()
    shutdownState.reset()


def _wait(qtbot, predicate, timeout=4000):
    qtbot.waitUntil(predicate, timeout=timeout)


def test_prepare_shutdown_drains_a_cooperative_script_and_closes_the_gate(scripting, qtbot):
    controller, executor, target, marks = scripting
    result = executor.execute(None, "marks.append('start')\ncancellableSleep(30)")
    _wait(qtbot, lambda: 'start' in marks)
    assert controller.prepareShutdown() is True
    assert shutdownState.scriptingDrained is True
    assert shutdownState.hardwareFinalizationAllowed()
    assert not apiGate.isOpen()
    assert result.status == ScriptRunStatus.CANCELLED
    assert not executor.isExecuting()


def test_a_queued_api_call_is_skipped_while_the_drain_pumps_events(scripting, qtbot):
    controller, executor, target, marks = scripting
    # The script blocks inside a queued UI-thread call; the GUI thread is
    # busy (this test) so it stays queued until the drain loop pumps events,
    # by which time the gate is closed: it must be skipped, not executed.
    result = executor.execute(None, """
marks.append('start')
try:
    api.poke()
except BaseException as e:            # OperationCancelled (withdrawn) or RuntimeError (gate)
    marks.append(type(e).__name__)
""")
    import time
    deadline = time.monotonic() + 3
    while 'start' not in marks and time.monotonic() < deadline:
        time.sleep(0.005)               # no event pumping here
    time.sleep(0.1)                     # let the script reach the queued call
    assert controller.prepareShutdown() is True
    assert target.calls == []                       # never executed
    assert marks[-1] in ('OperationCancelled', 'RuntimeError')
    assert result.status == ScriptRunStatus.CANCELLED


def test_a_script_that_swallows_everything_trips_the_cap_and_fails_closed(scripting, qtbot):
    controller, executor, target, marks = scripting
    controller.SHUTDOWN_DRAIN_CAP_MS = 400
    executor.execute(None, """
import time
marks.append('start')
while 'stop' not in marks:            # swallows every cancellation until told
    try:
        time.sleep(0.05)
    except BaseException:
        pass
""")
    _wait(qtbot, lambda: 'start' in marks)
    import time
    t0 = time.monotonic()
    assert controller.prepareShutdown() is False
    assert time.monotonic() - t0 < 2.0
    assert shutdownState.scriptingDrained is False
    assert not shutdownState.hardwareFinalizationAllowed()
    assert shutdownState.reasons and 'did not stop' in shutdownState.reasons[0]
    assert executor.isLeaked()
    marks.append('stop')                 # let the thread end so the test process can
    _wait(qtbot, lambda: not executor.isExecuting(), timeout=4000)


def test_prepare_shutdown_without_an_executor_reports_drained():
    shutdownState.reset()
    apiGate.reopen()
    controller = ImScrMainController.__new__(ImScrMainController)
    assert controller.prepareShutdown() is True
    assert shutdownState.scriptingDrained is True
    apiGate.reopen()
    shutdownState.reset()

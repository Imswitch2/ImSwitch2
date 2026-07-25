"""Unit tests for the deferred repeat-scan re-arm (crash fix).

Running the next repeat frame directly from inside the sigScanDone handler
re-enters nidaqManager.runScan while the just-finished scan's NI-DAQ tasks,
WaitThreads and detector QThreads are still tearing down — which crashes on
hardware when 'Repeat' is enabled. SuperScanController._armRepeatScan defers
the re-arm to the next event-loop turn and guards it against abort / repeat
being switched off in the gap.

These tests borrow the real methods onto a lightweight stub so the actual
control flow is exercised without constructing the full Qt controller stack.
"""

import pytest

from imswitch.imcontrol.controller import basecontrollers
from imswitch.imcontrol.controller.basecontrollers import SuperScanController


class _FakeWidget:
    def __init__(self, repeat=True):
        self._repeat = repeat

    def repeatEnabled(self):
        return self._repeat


class _StubController:
    """Borrows the real re-arm methods; stubs everything they touch."""

    _armRepeatScan = SuperScanController._armRepeatScan
    _fireRepeatScan = SuperScanController._fireRepeatScan
    _shouldContinueRepeat = SuperScanController._shouldContinueRepeat
    abortScan = SuperScanController.abortScan

    def __init__(self, repeat=True):
        self._repeatPending = False
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget = _FakeWidget(repeat=repeat)
        self.runCalls = []
        self.scanFailedCalls = 0

    def runScanAdvanced(self, *, sigScanStartingEmitted):
        self.runCalls.append(sigScanStartingEmitted)

    def scanFailed(self):
        self.scanFailedCalls += 1


@pytest.fixture
def captured_singleshot(monkeypatch):
    """Capture QTimer.singleShot callbacks instead of scheduling them."""
    calls = []

    def fake_single_shot(msec, callback):
        calls.append((msec, callback))

    monkeypatch.setattr(
        basecontrollers.QtCore.QTimer, 'singleShot', staticmethod(fake_single_shot)
    )
    return calls


def test_arm_defers_instead_of_running_immediately(captured_singleshot):
    ctrl = _StubController(repeat=True)
    ctrl._armRepeatScan()

    # Nothing ran synchronously; a zero-delay callback was scheduled.
    assert ctrl.runCalls == []
    assert ctrl._repeatPending is True
    assert len(captured_singleshot) == 1
    assert captured_singleshot[0][0] == 0


def test_deferred_callback_runs_next_frame(captured_singleshot):
    ctrl = _StubController(repeat=True)
    ctrl._armRepeatScan()

    _, callback = captured_singleshot[0]
    callback()

    assert ctrl.runCalls == [True]  # sigScanStartingEmitted=True
    assert ctrl._repeatPending is False


def test_abort_in_gap_cancels_pending_frame(captured_singleshot):
    ctrl = _StubController(repeat=True)
    ctrl.isRunning = True  # so abortScan doesn't fall through to scanFailed
    ctrl._armRepeatScan()

    ctrl.abortScan()  # user aborts before the deferred callback fires
    assert ctrl._repeatPending is False

    _, callback = captured_singleshot[0]
    callback()
    assert ctrl.runCalls == []  # no re-arm after abort


def test_unchecking_repeat_in_gap_cancels_frame(captured_singleshot):
    ctrl = _StubController(repeat=True)
    ctrl._armRepeatScan()

    ctrl._widget._repeat = False  # user unchecks 'Repeat' during the gap
    _, callback = captured_singleshot[0]
    callback()
    assert ctrl.runCalls == []


def test_does_not_stack_when_already_running(captured_singleshot):
    ctrl = _StubController(repeat=True)
    ctrl._armRepeatScan()

    ctrl.isRunning = True  # a scan started again by some other path
    _, callback = captured_singleshot[0]
    callback()
    assert ctrl.runCalls == []


def test_continuous_mode_override_keeps_looping(captured_singleshot):
    """Controllers that also loop in continuous-laser mode override the guard."""

    class _ContWidget(_FakeWidget):
        def __init__(self):
            super().__init__(repeat=False)
            self._cont = True

        def isContLaserMode(self):
            return self._cont

    class _ContController(_StubController):
        def _shouldContinueRepeat(self):
            return self._widget.isContLaserMode() or self._widget.repeatEnabled()

    ctrl = _ContController(repeat=False)
    ctrl._widget = _ContWidget()
    ctrl._armRepeatScan()

    _, callback = captured_singleshot[0]
    callback()
    # repeat is off but continuous-laser mode is on -> still re-arms.
    assert ctrl.runCalls == [True]

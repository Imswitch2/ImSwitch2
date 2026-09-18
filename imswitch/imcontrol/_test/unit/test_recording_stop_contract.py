"""stopRecording() is idempotent and says whether a terminal will follow
(plan A-09, R-11); isRecording() exposes the flag."""
from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)


class _Widget:
    def __init__(self):
        self.calls = []

    def setRecButtonChecked(self, checked):
        self.calls.append(checked)


def _controller(recording):
    ctrl = RecordingController.__new__(RecordingController)
    ctrl._widget = _Widget()
    ctrl.recording = recording
    return ctrl


def test_stop_when_nothing_is_recording_returns_false():
    for state in (False, None):  # never started / already ended / failed start
        ctrl = _controller(state)
        assert ctrl.isRecording() is False
        assert ctrl.stopRecording() is False
        assert ctrl._widget.calls == [False]      # harmless, nothing toggles


def test_stop_while_recording_returns_true_and_unchecks_the_button():
    ctrl = _controller(True)
    assert ctrl.isRecording() is True
    assert ctrl.stopRecording() is True
    assert ctrl._widget.calls == [False]


def test_the_exports_run_on_the_ui_thread():
    assert RecordingController.stopRecording._APIRunOnUIThread is True
    assert RecordingController.isRecording._APIRunOnUIThread is True


# ---------------------------------------------------------------------------
# REC-off does not block the GUI thread on the writer drain; the worker's own
# terminal resets the controller. A re-check during the drain is refused.
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from imswitch.imcontrol.model import RecMode


class _FakeManager:
    def __init__(self):
        self.calls = []
        self.record = True

    def endRecording(self, emitSignal=True, wait=True):
        self.calls.append({'emitSignal': emitSignal, 'wait': wait})


def _until_stop_controller(recording=True):
    ctrl = RecordingController.__new__(RecordingController)
    ctrl._widget = _Widget()
    ctrl._master = SimpleNamespace(recordingManager=_FakeManager())
    ctrl._commChannel = SimpleNamespace(getActiveScanSource=lambda: None)
    ctrl._RecordingController__logger = SimpleNamespace(
        info=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    ctrl.recMode = RecMode.UntilStop
    ctrl.lapseCurrent = -1
    ctrl.timer = None
    ctrl.recording = recording
    ctrl._finalizingRecCycle = False
    ctrl._awaitingScanSourceArm = False
    return ctrl


def test_rec_off_ends_the_recording_without_waiting_or_emitting():
    ctrl = _until_stop_controller(recording=True)
    ctrl.toggleREC(False)
    assert ctrl._master.recordingManager.calls == [{'emitSignal': False, 'wait': False}]
    assert ctrl.recording is True          # reset by the worker's recordingEnded, not here


def test_rec_on_while_the_previous_recording_drains_is_refused():
    ctrl = _until_stop_controller(recording=True)
    ctrl.toggleREC(True)
    assert ctrl._master.recordingManager.calls == []
    assert ctrl._widget.calls == [False]   # button put back, without a stop
    assert ctrl._finalizingRecCycle is False

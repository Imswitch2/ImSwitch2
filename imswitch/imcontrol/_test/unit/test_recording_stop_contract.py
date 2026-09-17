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

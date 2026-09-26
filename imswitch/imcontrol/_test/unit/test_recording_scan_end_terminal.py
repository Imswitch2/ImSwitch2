"""A scan recording publishes ``recordingEnded`` once, when it is finished.

The recording worker holds the legacy ``sigRecordingEnded`` back in the scan
modes (48a6ae31, 2022): back then the controller ended its own cycle on it,
and a scan cycle has to end on the scan. The controller has since moved to the
detailed terminal, and nothing published the public signal for a completed
scan-once recording any more -- a script waiting for it hung, and the
positioner joystick, re-enabled on it, stayed disabled.

``recordingCycleEnded`` runs once the writer has drained *and* the scan has
ended, so that is where the scan modes' public terminal is published.
"""

import types

import pytest

pytest.importorskip("qtpy")

from imswitch.imcontrol.controller.controllers.RecordingController import (  # noqa: E402
    RecordingController,
)
from imswitch.imcontrol.model import RecMode  # noqa: E402


class _Signal:
    def __init__(self):
        self.emit_calls = 0

    def emit(self, *_args, **_kwargs):
        self.emit_calls += 1


class _Widget:
    def __getattr__(self, _name):
        # recordingCycleEnded's UI reset is best effort; any call is fine.
        return lambda *_args, **_kwargs: None

    def isRecButtonChecked(self):
        return False


def _controller(recMode, *, failed=False):
    ctrl = types.SimpleNamespace(
        recMode=recMode,
        recording=True,
        stopRequested=False,
        lapseCurrent=-1,
        lapseTotal=0,
        timer=None,
        doneScan=True,
        endedRecording=True,
        _recordingCycleTerminalHandled=False,
        _recordingFailedCurrent=failed,
        _finalizingRecCycle=False,
        _widget=_Widget(),
        _commChannel=types.SimpleNamespace(sigRecordingEnded=_Signal()),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    return ctrl


def test_completed_scan_once_recording_publishes_recording_ended_once():
    ctrl = _controller(RecMode.ScanOnce)

    RecordingController.recordingCycleEnded(ctrl)

    assert ctrl._commChannel.sigRecordingEnded.emit_calls == 1
    assert ctrl.recording is False


def test_a_second_terminal_for_the_same_cycle_does_not_publish_again():
    # The fallback wiring routes sigRecordingEnded back into recordingEnded()
    # -> recordingCycleEnded(); the cycle guard must absorb that re-entry.
    ctrl = _controller(RecMode.ScanOnce)

    RecordingController.recordingCycleEnded(ctrl)
    RecordingController.recordingCycleEnded(ctrl)

    assert ctrl._commChannel.sigRecordingEnded.emit_calls == 1


def test_failed_scan_recording_does_not_claim_success():
    # recordingFailed was published for it; recordingEnded would be a lie.
    ctrl = _controller(RecMode.ScanOnce, failed=True)

    RecordingController.recordingCycleEnded(ctrl)

    assert ctrl._commChannel.sigRecordingEnded.emit_calls == 0


@pytest.mark.parametrize("recMode", [RecMode.SpecFrames, RecMode.SpecTime, RecMode.UntilStop])
def test_non_scan_modes_are_left_to_the_worker(recMode):
    # These get the legacy signal from RecordingManager.endRecording();
    # publishing it here as well would announce the end twice.
    ctrl = _controller(recMode)

    RecordingController.recordingCycleEnded(ctrl)

    assert ctrl._commChannel.sigRecordingEnded.emit_calls == 0

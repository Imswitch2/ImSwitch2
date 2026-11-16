"""A stopped scan ends its recording with what it completed (plan
scan-stop-now, Lenny 2026-10-18, decision 2).

The scan's unfinished frame never reaches the writer (its detectors drop
it); the frames before it are kept, the writer finalizes them as
``stopped_early``. Before: a scan stopped from its own panel failed the
recording ("Scan ended before acquisition completed"), and the Recording
panel's Stop of a scan lapse aborted the writer, deleting the file.
"""
import types

from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
from imswitch.imcontrol.model import RecMode


def _controller(recMode, *, completion=None, source=None, events):
    manager = types.SimpleNamespace(
        record=True,
        endRecording=lambda **kwargs: events.append(('end-writer', kwargs)),
        abortRecording=lambda **kwargs: events.append(('abort-writer', kwargs)),
    )
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=completion,
        _acceptedScanRunToken=None,
        _exactScanCompletionHandled=False,
        _scanStartPublished=False,
        _scanStartOwnedByScanSource=False,
        _scanRequestAccepted=True,
        _scanLifecycleEndedObserved=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailureHandled=False,
        _recordingScanSource=source,
        _shutdownRequested=False,
        _usesDetailedRecordingSignals=True,
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *a, **k: events.append(('error', a))),
        _master=types.SimpleNamespace(recordingManager=manager,
                                      scanExecutionCoordinator=None),
        stopRequested=False,
        recMode=recMode,
        lapseCurrent=0,
        lapseTotal=3,
        timer=None,
        doneScan=False,
        endedRecording=False,
        recordingCycleEnded=lambda: events.append(('cycle-ended',)),
    )
    ctrl._handleRecordingFailure = lambda message, **kwargs: events.append(('failed', message))
    for name in ('_endForStoppedScan', '_stopManagerRecordingInBackground'):
        setattr(ctrl, name, types.MethodType(getattr(RecordingController, name), ctrl))
    return ctrl


class _Completion:
    successful = False
    message = 'Scan was stopped.'
    stopped = True

    def wait(self, timeout=None):
        return True


def test_a_scan_stopped_from_its_panel_ends_the_recording_it_was_bound_to():
    events = []
    completion = _Completion()
    ctrl = _controller(RecMode.ScanOnce, completion=completion, events=events)
    RecordingController._exactScanRequestCompleted(ctrl, completion)
    assert ('end-writer', {'emitSignal': False, 'wait': False}) in events   # keeps its frames
    assert not any(event[0] in ('failed', 'abort-writer') for event in events)
    assert ctrl.stopRequested is True and ctrl.doneScan is True


def test_a_stopped_lapse_part_ends_the_lapse_and_does_not_go_on():
    events = []
    completion = _Completion()
    ctrl = _controller(RecMode.ScanLapse, completion=completion, events=events)

    class _Timer:
        def stop(self):
            events.append(('timer-stopped',))
    ctrl.timer = _Timer()
    RecordingController._exactScanRequestCompleted(ctrl, completion)
    assert ('timer-stopped',) in events and ctrl.timer is None
    assert ('end-writer', {'emitSignal': False, 'wait': False}) in events
    assert not any(event[0] == 'failed' for event in events)


def test_a_run_that_failed_still_fails_its_recording():
    events = []

    class _Failed(_Completion):
        stopped = False
        message = 'NI-DAQ task "ao" failed'
    completion = _Failed()
    ctrl = _controller(RecMode.ScanOnce, completion=completion, events=events)
    RecordingController._exactScanRequestCompleted(ctrl, completion)
    assert ('failed', 'NI-DAQ task "ao" failed') in events


def test_the_global_scan_end_of_a_stopped_run_ends_the_recording_too():
    """A recording armed before the operator starts the scan from its panel
    hears only the global end: it asks the source whether it was stopped."""
    events = []
    source = types.SimpleNamespace(isRunning=False, runWasStopped=lambda: True)
    ctrl = _controller(RecMode.ScanOnce, source=source, events=events)
    ctrl._scanStartOwnedByScanSource = True
    for name in ('_scanSourceWasStopped',):
        setattr(ctrl, name, types.MethodType(getattr(RecordingController, name), ctrl))
    RecordingController._scanLifecycleEnded(ctrl)
    assert ('end-writer', {'emitSignal': False, 'wait': False}) in events
    assert not any(event[0] == 'failed' for event in events)

    events.clear()
    failed = _controller(RecMode.ScanOnce, events=events,
                         source=types.SimpleNamespace(isRunning=False,
                                                      runWasStopped=lambda: False))
    failed._scanStartOwnedByScanSource = True
    RecordingController._scanLifecycleEnded(failed)
    assert ('failed', 'Scan ended before acquisition completed.') in events


def test_the_recording_panels_stop_of_a_scan_lapse_keeps_its_frames():
    """REC off during a recorded scan lapse: the writer ends (its complete
    timepoints kept, stopped_early); it is no longer aborted."""
    events = []
    ctrl = _controller(RecMode.ScanLapse, events=events)
    ctrl.recording = True
    ctrl._finalizingRecCycle = False
    ctrl._recordingCycleTerminalHandled = False
    ctrl._scanRunIsActive = lambda: True
    ctrl._abortOwnedScanSequence = lambda: events.append(('stop-scan',))
    ctrl._notifyScanEndedIfPending = lambda: None
    RecordingController.toggleREC(ctrl, False)
    assert ('end-writer', {'emitSignal': False, 'wait': True}) in events
    assert ('stop-scan',) in events
    assert not any(event[0] == 'abort-writer' for event in events)


def test_a_stopped_scans_flim_frame_is_not_read_as_final():
    """The NI-DAQ sends stopped, then done: the Time Tagger must not wake its
    worker to read the unfinished FLIM frame as the final one (the abort
    that ends the run tears the worker down and drops it)."""
    from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
        SwabianTimeTaggerManager,
    )
    woken = []
    tagger = SwabianTimeTaggerManager.__new__(SwabianTimeTaggerManager)
    tagger.__dict__.update(_scanParticipating=True, acquisition=True, _scanStopped=False,
                           _scanWorker=types.SimpleNamespace(
                               signal_done=lambda: woken.append(True)))
    SwabianTimeTaggerManager._onScanStopped(tagger)
    SwabianTimeTaggerManager._onScanDone(tagger)
    assert woken == [] and tagger.acquisition is False

    tagger._scanStopped = False                       # a finished scan: read it
    tagger.acquisition = True
    SwabianTimeTaggerManager._onScanDone(tagger)
    assert woken == [True]

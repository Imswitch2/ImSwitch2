"""RecordingController.recordCameraSeries: a caller's camera lapse.

The camera twin of recordScanSeries. These tests drive the real REC
transaction (toggleREC -> nextCameraLapse -> recordingCycleEnded) against the
doubles in conftest.py and check what the caller gets and what the Recording
widget is spared.
"""
import pytest

from imswitch.imcontrol.controller.controllers.RecordingController import (
    ScanSeriesRecordingResult,
)
from imswitch.imcontrol.model import RecMode, SaveFormat
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)


def _nothingChanged(h):
    assert h.ctrl._seriesRequest is None
    assert h.ctrl.recMode == RecMode.SpecFrames
    assert h.widget.checked is False
    assert h.manager.started == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('kwargs, reason', [
    ({'timepoints': 0}, 'at least 1'),
    ({'timepoints': 2.5}, 'whole number'),
    ({'timepoints': 'x'}, 'whole number'),
    ({'timepoints': 2, 'framesPerPoint': 0}, 'at least 1'),
    ({'timepoints': 2, 'framesPerPoint': 1.5}, 'whole number'),
    ({'timepoints': 2, 'intervalSeconds': -1}, 'interval'),
    ({'timepoints': 2, 'intervalSeconds': float('inf')}, 'interval'),
    ({'timepoints': 2, 'intervalSeconds': 'soon'}, 'interval'),
])
def test_bad_numbers_are_refused(harness, kwargs, reason):
    result = harness.ctrl.recordCameraSeries(['CamA'], **kwargs)
    assert result.accepted is False
    assert reason in result.reason
    _nothingChanged(harness)


def test_no_detector_is_refused(harness):
    result = harness.ctrl.recordCameraSeries([], 3)
    assert result == ScanSeriesRecordingResult(
        False, 'Choose at least one detector to save.'
    )
    _nothingChanged(harness)


@pytest.mark.parametrize('state', [
    {'recording': True},
    {'_awaitingScanSourceArm': True},
    {'_seriesRequest': object()},
])
def test_a_running_recording_refuses_a_second(harness, state):
    harness.ctrl.__dict__.update(state)
    result = harness.ctrl.recordCameraSeries(['CamA'], 3)
    assert result.accepted is False
    assert 'already running' in result.reason
    assert harness.manager.started == []


def test_a_checked_rec_button_is_a_running_recording(harness):
    harness.widget.checked = True
    result = harness.ctrl.recordCameraSeries(['CamA'], 3)
    assert result.accepted is False
    assert harness.manager.started == []


def test_an_acquisition_owned_by_a_scan_widget_is_refused(harness):
    harness.ctrl._commChannel.recordingWorkflow = type(
        'W', (), {'recording_acquisition_owner': lambda self: object()}
    )()
    result = harness.ctrl.recordCameraSeries(['CamA'], 3)
    assert result.accepted is False
    assert 'another scan widget' in result.reason
    _nothingChanged(harness)


def test_a_grouped_tiff_lapse_is_refused(harness_factory):
    h = harness_factory(saveFormat=SaveFormat.TIFF)
    result = h.ctrl.recordCameraSeries(['CamA'], 3, singleFile=True)
    assert result.accepted is False
    assert 'HDF5 and ZARR' in result.reason
    _nothingChanged(h)
    assert h.ctrl.recordCameraSeries(
        ['CamA'], 3, singleFile=False
    ).accepted is True


def test_a_scan_driven_detector_is_refused_with_the_validation_reason(harness):
    result = harness.ctrl.recordCameraSeries(['CamA', 'Scanned'], 3)
    assert result.accepted is False
    assert 'scan-driven' in result.reason and 'Scanned' in result.reason
    _nothingChanged(harness)


def test_an_externally_triggered_detector_is_refused(harness):
    harness.detectors['CamA'].parameters = {
        'Trigger source': 'External "frame-trigger"'
    }
    result = harness.ctrl.recordCameraSeries(['CamA'], 3)
    assert result.accepted is False
    assert 'internal/free-running' in result.reason
    _nothingChanged(harness)


# ---------------------------------------------------------------------------
# Accepted: what the REC press sees, and what the widget is spared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('timepoints', [1, 4])
def test_the_request_is_a_camera_lapse_for_the_callers_detectors(
        harness, timepoints):
    result = harness.ctrl.recordCameraSeries(
        ['CamA', 'CamB'], timepoints, 2.5, singleFile=False,
        framesPerPoint=3, owner='me',
    )
    assert result == ScanSeriesRecordingResult(True)
    ctrl = harness.ctrl
    request = ctrl._seriesRequest
    assert request.kind == 'camera'
    assert request.source is None and request.owner == 'me'
    assert (request.frames, request.intervalSeconds, request.framesPerPoint) \
        == (timepoints, 2.5, 3)
    assert ctrl.recMode == RecMode.CameraLapse
    assert harness.widget.checked is True

    started = harness.manager.started[0]
    assert started['detectorNames'] == ['CamA', 'CamB']
    assert started['recFrames'] == 3
    assert started['recLapseTotal'] == timepoints
    assert started['recLapseIndex'] == 0
    assert started['recLapseIntervalS'] == 2.5
    assert started['singleLapseFile'] is False
    assert started['recMode'] == RecMode.CameraLapse
    assert started['savename'].endswith('_time0')
    # No scan source, so nothing is announced as scan-series activity and
    # nothing of the widget's own lapse settings or detector choice is read.
    assert harness.widget.settingReads == []


def test_the_shared_attrs_report_the_request_not_the_widget(harness):
    harness.ctrl.recordCameraSeries(['CamA'], 5, 7.0)
    attrs = {key[1]: value for key, value in harness.sharedAttrs.items()}
    assert 5 in attrs.values() and 7.0 in attrs.values()
    assert 99 not in attrs.values() and 99.0 not in attrs.values()


def test_the_widget_is_left_as_the_user_set_it_afterwards(harness):
    harness.ctrl.recordCameraSeries(['CamA'], 1)
    assert harness.ctrl.recMode == RecMode.CameraLapse
    harness.finishPoint()
    assert harness.ctrl.recMode == RecMode.SpecFrames
    assert harness.ctrl._seriesRequest is None
    assert harness.widget.checked is False
    assert harness.widget.settingReads == []


def test_every_point_records_the_requested_frames_per_point(harness):
    harness.ctrl.recordCameraSeries(['CamA'], 3, 0.0, framesPerPoint=4)
    for _ in range(2):
        harness.finishPoint()
        harness.timers[-1].fire()
    frames = [args['recFrames'] for args in harness.manager.started]
    assert frames == [4, 4, 4]
    assert [a['recLapseIndex'] for a in harness.manager.started] == [0, 1, 2]


def test_the_default_is_one_frame_per_point(harness):
    harness.ctrl.recordCameraSeries(['CamA'], 2)
    assert harness.manager.started[0]['recFrames'] == 1


def test_a_widget_started_camera_lapse_is_unchanged(harness):
    """No request: the widget's numbers, one frame per point, no gate."""
    ctrl = harness.ctrl
    ctrl.recMode = RecMode.CameraLapse
    harness.widget.getTimelapseNumFrames = lambda: 2
    harness.widget.getSpecTimelapseFrameTime = lambda: 1.5
    harness.widget.getTimelapseSingleFile = lambda: True
    harness.widget.getDetectorMode = lambda: -3
    harness.widget.getSelectedSpecificDetectors = lambda: ['CamB']
    harness.widget.setRecButtonChecked(True)
    started = harness.manager.started[0]
    assert started['detectorNames'] == ['CamB']
    assert started['recFrames'] == 1
    assert started['recLapseTotal'] == 2
    assert started['recLapseIntervalS'] == 1.5
    assert started['singleLapseFile'] is True


# ---------------------------------------------------------------------------
# The positioning gate on a camera lapse
# ---------------------------------------------------------------------------

def _gate(harness, requests):
    asked = []

    def provider(index):
        asked.append(index)
        request = requests[index]
        return request

    harness.ctrl.setPositioningProvider(provider)
    return asked


def test_a_pending_gate_defers_arming_and_retries_the_camera_lapse(harness):
    pending = PositioningRequest(0, timeout_s=60, attributes={'tile': 7})
    asked = _gate(harness, {0: pending})
    result = harness.ctrl.recordCameraSeries(['CamA'], 2)

    assert harness.manager.started == []
    retry = harness.timers[-1]
    assert retry.active and retry.interval == 25
    assert retry.callbacks == [harness.ctrl.nextCameraLapse]
    assert result.accepted is True
    assert asked == [0]

    pending.resolve()
    retry.fire()
    assert asked == [0], 'the provider is asked once per point'
    started = harness.manager.started[0]
    assert started['attrs'] == {'CamA': {'base': 'attr', 'tile': 7}}


def test_the_attribute_snapshot_is_taken_when_the_gate_releases(harness):
    """A timelapse gate switches the lasers on right before releasing the
    point; the point's attributes must describe that state, not the one when
    Recording first asked."""
    state = {'laser': 'off', 'snapshots': 0}

    def snapshot(inner):
        state['snapshots'] += 1
        return {'laser': state['laser']}

    harness.sharedAttrs.__class__.getHDF5Attributes = snapshot
    pending = PositioningRequest(0, timeout_s=60)
    _gate(harness, {0: pending})
    harness.ctrl.recordCameraSeries(['CamA'], 2)
    afterStart = state['snapshots']
    for _ in range(3):
        harness.timers[-1].fire()  # still pending
    assert harness.manager.started == []
    assert state['snapshots'] == afterStart, (
        'a pending gate retry must not snapshot the point'
    )

    state['laser'] = 'on'  # what the gate does just before releasing
    pending.resolve()
    harness.timers[-1].fire()
    assert harness.manager.started[0]['attrs']['CamA'] == {'laser': 'on'}


def test_gate_attributes_apply_to_that_point_only(harness):
    first = PositioningRequest(0, attributes={'tile': 1})
    second = PositioningRequest(1)
    first.resolve()
    second.resolve()
    _gate(harness, {0: first, 1: second})
    harness.ctrl.recordCameraSeries(['CamA'], 2)
    harness.finishPoint()
    harness.timers[-1].fire()
    attrs = [a['attrs']['CamA'] for a in harness.manager.started]
    assert attrs == [{'base': 'attr', 'tile': 1}, {'base': 'attr'}]


def test_a_provider_declining_a_point_leaves_attrs_alone(harness):
    first = PositioningRequest(0, attributes={'tile': 1})
    first.resolve()
    _gate(harness, {0: first, 1: None})
    harness.ctrl.recordCameraSeries(['CamA'], 2)
    harness.finishPoint()
    harness.timers[-1].fire()
    assert harness.manager.started[1]['attrs']['CamA'] == {'base': 'attr'}


@pytest.mark.parametrize('settle, word', [
    (lambda r: r.fail('stage faulted'), 'failed'),
    (lambda r: r.cancel('operator stopped'), 'cancelled'),
])
def test_a_gate_that_did_not_resolve_fails_the_series(
        harness, settle, word):
    request = PositioningRequest(0, timeout_s=60)
    asked = _gate(harness, {0: request})
    harness.ctrl.recordCameraSeries(['CamA'], 2)
    ended = []
    harness.ctrl._seriesOnEnded = lambda failed, message: ended.append(
        (failed, message))
    settle(request)
    harness.timers[-1].fire()

    assert harness.manager.started == []
    assert harness.ctrl._seriesRequest is None
    assert len(ended) == 1
    failed, message = ended[0]
    assert failed is True and word in message
    assert asked == [0]

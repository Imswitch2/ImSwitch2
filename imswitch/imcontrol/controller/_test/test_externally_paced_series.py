"""Externally paced series, the end-of-series callback and the graceful stop.

Driven through the real REC transaction against the doubles in conftest.py.
"""
import pytest

from imswitch.imcontrol.model import RecMode
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)

NEEDS_PROVIDER = (
    'An externally paced series needs a positioning provider to hold each '
    'point until it is due.'
)


def _resolvedGate(harness, attributes=None):
    def provider(index):
        request = PositioningRequest(index, attributes=attributes)
        request.resolve()
        return request

    harness.ctrl.setPositioningProvider(provider)


def _recordEnds(harness):
    ended = []
    return ended, (lambda failed, message: ended.append((failed, message)))


# ---------------------------------------------------------------------------
# Externally paced
# ---------------------------------------------------------------------------

def test_an_externally_paced_camera_series_needs_a_provider(harness):
    result = harness.ctrl.recordCameraSeries(
        ['CamA'], 3, 30.0, externallyPaced=True)
    assert result.accepted is False and result.reason == NEEDS_PROVIDER
    assert harness.ctrl._seriesRequest is None
    assert harness.manager.started == []


def test_an_externally_paced_scan_series_needs_a_provider(harness):
    result = harness.ctrl.recordScanSeries(
        object(), ['APD'], 3, 30.0, externallyPaced=True)
    assert result.accepted is False and result.reason == NEEDS_PROVIDER
    assert harness.ctrl._seriesRequest is None


def test_the_next_camera_point_is_due_at_once_but_reports_the_interval(
        harness):
    _resolvedGate(harness)
    harness.ctrl.recordCameraSeries(
        ['CamA'], 3, 30.0, externallyPaced=True)
    harness.finishPoint()

    timer = harness.timers[-1]
    assert timer.active and timer.interval == 0
    timer.fire()
    started = harness.manager.started
    assert len(started) == 2
    assert started[1]['recLapseIntervalS'] == 30.0
    shared = {k[1]: v for k, v in harness.sharedAttrs.items()}
    assert 30.0 in shared.values()


def test_an_ordinary_camera_series_still_waits_its_interval(harness):
    harness.ctrl.recordCameraSeries(['CamA'], 3, 30.0)
    harness.finishPoint()
    assert harness.timers[-1].interval > 0


def test_the_scheduled_time_is_when_the_gate_released_the_point(harness):
    pending = PositioningRequest(1, timeout_s=60)
    first = PositioningRequest(0)
    first.resolve()
    harness.ctrl.setPositioningProvider(
        lambda index: first if index == 0 else pending)
    harness.ctrl.recordCameraSeries(
        ['CamA'], 2, 30.0, externallyPaced=True)
    asked = harness.manager.started[0]['recLapseScheduledTime']
    harness.finishPoint()
    harness.timers[-1].fire()
    assert len(harness.manager.started) == 1, 'held by the gate'
    pending.resolve()
    harness.timers[-1].fire()
    released = harness.manager.started[1]['recLapseScheduledTime']
    assert released is not None and released >= asked


def test_an_externally_paced_scan_lapse_schedules_with_zero_delay(harness):
    ctrl = harness.ctrl
    _resolvedGate(harness)
    from types import SimpleNamespace
    ctrl.__dict__.update(
        _seriesRequest=SimpleNamespace(
            kind='scan', source=object(), intervalSeconds=30.0,
            externallyPaced=True, frames=3, singleFile=True,
            previousMode=RecMode.SpecFrames, owner=None,
        ),
        recMode=RecMode.ScanLapse, lapseCurrent=0, lapseTotal=3,
        stopRequested=False,
    )
    harness.widget.checked = True
    harness.manager.record = False
    ctrl.recordingCycleEnded()
    timer = harness.timers[-1]
    assert timer.interval == 0
    assert timer.callbacks == [ctrl.nextLapse]


def _firstScanPoint(harness, *, pending, failed=False):
    """recordScanSeries with nextLapse standing in for the first point."""
    ctrl = harness.ctrl
    ctrl.setPositioningProvider(lambda index: None)

    def nextLapse():
        if pending:
            ctrl._pendingPositioning = PositioningRequest(0, timeout_s=60)
        if failed:
            ctrl._recordingFailedCurrent = True
            ctrl._lastRecordingFailure = 'arm failed'
        return False

    ctrl.nextLapse = nextLapse
    return ctrl.recordScanSeries(
        object(), ['APD'], 3, 30.0, externallyPaced=True)


def test_a_first_scan_point_held_by_its_gate_is_a_started_series(harness):
    """The gate holding point 0 is waiting, not failure: refusing here left
    the gate's retry timer running a lapse with no series request, which then
    recorded with the Recording widget's own settings."""
    result = _firstScanPoint(harness, pending=True)
    assert result.accepted is True
    assert harness.ctrl.recording is True
    assert harness.ctrl._seriesRequest is not None


def test_a_failed_first_scan_point_is_still_refused(harness):
    result = _firstScanPoint(harness, pending=True, failed=True)
    assert result.accepted is False
    assert harness.ctrl.recording is False


# ---------------------------------------------------------------------------
# onEnded
# ---------------------------------------------------------------------------

def test_on_ended_fires_once_when_the_series_completes(harness):
    ended, callback = _recordEnds(harness)
    harness.ctrl.recordCameraSeries(['CamA'], 2, 0.0, onEnded=callback)
    harness.finishPoint()
    assert ended == []
    harness.timers[-1].fire()
    harness.finishPoint()
    assert ended == [(False, '')]
    assert harness.ctrl._seriesRequest is None
    assert harness.ctrl._seriesOnEnded is None


def test_on_ended_reports_a_failure_with_its_reason(harness):
    ended, callback = _recordEnds(harness)
    harness.ctrl.recordCameraSeries(['CamA'], 3, 0.0, onEnded=callback)
    harness.ctrl.recordingFailed('disk full')
    assert ended == [(True, 'disk full')]


def test_on_ended_for_a_gate_failure(harness):
    ended, callback = _recordEnds(harness)
    request = PositioningRequest(0)
    request.fail('stage faulted')
    harness.ctrl.setPositioningProvider(lambda _i: request)
    result = harness.ctrl.recordCameraSeries(
        ['CamA'], 2, onEnded=callback)
    # The failure happened while starting, so it is the caller's refusal.
    assert result.accepted is False
    assert 'stage faulted' in result.reason
    assert ended == []


def test_on_ended_never_fires_for_a_refused_start(harness):
    ended, callback = _recordEnds(harness)
    assert harness.ctrl.recordCameraSeries(
        [], 3, onEnded=callback).accepted is False
    assert harness.ctrl.recordCameraSeries(
        ['CamA'], 3, externallyPaced=True, onEnded=callback
    ).accepted is False
    assert harness.ctrl.recordCameraSeries(
        ['Scanned'], 3, onEnded=callback).accepted is False
    assert ended == []
    assert harness.ctrl._seriesOnEnded is None


def test_an_exception_in_on_ended_is_contained(harness):
    def boom(failed, message):
        raise RuntimeError('listener bug')

    harness.ctrl.recordCameraSeries(['CamA'], 1, onEnded=boom)
    harness.finishPoint()
    assert harness.ctrl._seriesRequest is None


# ---------------------------------------------------------------------------
# stopSeries / getActiveSeriesInfo
# ---------------------------------------------------------------------------

def test_stop_series_between_points_ends_the_series_now(harness):
    ended, callback = _recordEnds(harness)
    harness.ctrl.recordCameraSeries(
        ['CamA'], 5, 60.0, owner='me', onEnded=callback)
    harness.finishPoint()
    assert harness.timers[-1].active

    assert harness.ctrl.stopSeries('me') is True
    assert ended == [(False, '')]
    assert harness.ctrl._seriesRequest is None
    assert harness.timers[-1].active is False
    assert len(harness.manager.started) == 1
    assert harness.manager.aborted == 0


def test_stop_series_during_a_point_lets_the_point_finish(harness):
    ended, callback = _recordEnds(harness)
    harness.ctrl.recordCameraSeries(
        ['CamA'], 5, 0.0, owner='me', onEnded=callback)
    assert harness.manager.record is True

    assert harness.ctrl.stopSeries('me') is True
    assert harness.manager.aborted == 0
    assert harness.manager.record is True
    assert ended == []

    harness.finishPoint()
    assert ended == [(False, '')]
    assert len(harness.manager.started) == 1


def test_stop_series_after_a_fired_retry_timer_does_not_abort_the_point(
        harness):
    """A fired single-shot timer stays referenced while its point runs."""
    pending = PositioningRequest(0, timeout_s=60)
    harness.ctrl.setPositioningProvider(lambda _i: pending)
    harness.ctrl.recordCameraSeries(['CamA'], 3, 0.0, owner='me')
    pending.resolve()
    harness.timers[-1].fire()
    assert harness.manager.record is True

    assert harness.ctrl.stopSeries('me') is True
    assert harness.manager.record is True
    assert harness.ctrl._seriesRequest is not None


def test_stop_series_belongs_to_its_owner(harness):
    assert harness.ctrl.stopSeries('me') is False
    harness.ctrl.recordCameraSeries(['CamA'], 5, 60.0, owner='me')
    assert harness.ctrl.stopSeries('someone else') is False
    assert harness.ctrl.stopRequested is False
    assert harness.ctrl.stopSeries('me') is True


def test_stop_series_for_a_scan_series_delegates_to_stop_scan_series(harness):
    from types import SimpleNamespace
    source = object()
    harness.ctrl._seriesRequest = SimpleNamespace(
        kind='scan', source=source, owner=source, frames=4)
    calls = []
    harness.ctrl.stopScanSeries = lambda s: calls.append(s) or True
    assert harness.ctrl.stopSeries(source) is True
    assert calls == [source]
    assert harness.ctrl.stopSeries(object()) is False


def test_active_series_info(harness):
    assert harness.ctrl.getActiveSeriesInfo() is None
    _resolvedGate(harness)
    harness.ctrl.recordCameraSeries(
        ['CamA'], 3, 10.0, externallyPaced=True)
    assert harness.ctrl.getActiveSeriesInfo() == {
        'kind': 'camera', 'timepoints': 3, 'current': 0,
        'externallyPaced': True,
    }
    harness.finishPoint()
    harness.timers[-1].fire()
    assert harness.ctrl.getActiveSeriesInfo()['current'] == 1
    harness.ctrl.stopSeries(None)
    harness.finishPoint()
    assert harness.ctrl.getActiveSeriesInfo() is None

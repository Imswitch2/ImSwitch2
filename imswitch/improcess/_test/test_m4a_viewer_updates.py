"""M4A: viewer-only partial T planes keep Session result identity and navigation.

No real napari or Qt widgets are needed: these tests exercise the production
controller methods with a fake list, viewer layers and time-slider callbacks.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

pytest.importorskip("qtpy")

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.execution.qt_workers import StreamProcessWorker


class _Entry:
    def __init__(self, result):
        self._result = result

    def data(self, _role):
        return self._result

    def setData(self, _role, value):
        self._result = value


class _List:
    def __init__(self):
        self.items = []
        self.selected = None

    def currentItem(self):
        return self.selected

    def row(self, item):
        return self.items.index(item) if item in self.items else -1


class _View:
    def __init__(self):
        self.reconList = _List()
        self.renders = []
        self.steps = []
        self.current_t = 0
        self.controller = None
        self.jump_on_swap = None
        self.resetView = Mock()
        self.removeData = Mock(return_value=True)

    def addNamedData(self, result, _name):
        entry = _Entry(result)
        self.reconList.items.append(entry)
        self.reconList.selected = entry
        return entry

    def tryFastLiveUpdate(self, result, _order):
        self.renders.append(np.array(result.data, copy=True))
        # Replacing napari image-layer data can reset its current_step and
        # synchronously emit a dims event. This is not a manual slider change.
        if self.jump_on_swap is not None:
            self.current_t = self.jump_on_swap
            self.controller.axisStepChanged((self.current_t, 0, 0))
        return True

    def moveDimStep(self, axis, step):
        self.steps.append((axis, step))
        self.current_t = int(step)
        self.controller.axisStepChanged((self.current_t, 0, 0))

    def getDimensionStepState(self):
        return {"T": self.current_t, "Y": 0, "X": 0}

    def userSelectTimepoint(self, timepoint):
        # A genuine Qt slider input is recognized *before* it changes napari's
        # Dims model. Internal layer-data updates never emit this signal.
        self.controller._liveUserDimsInteracted()
        self.current_t = int(timepoint)
        self.controller.axisStepChanged((self.current_t, 0, 0))

    def getCurrentItemData(self):
        entry = self.reconList.selected
        return entry.data(1) if entry is not None else None


class _Controller:
    liveResultUpdated = ReconstructionViewController.liveResultUpdated
    liveTimepointProvisional = ReconstructionViewController.liveTimepointProvisional
    _liveItemIsAlive = ReconstructionViewController._liveItemIsAlive
    _renderPendingLiveResult = ReconstructionViewController._renderPendingLiveResult
    _displayLiveUpdate = ReconstructionViewController._displayLiveUpdate
    _advanceLiveTimeSlider = ReconstructionViewController._advanceLiveTimeSlider
    _liveTimepointAxis = ReconstructionViewController._liveTimepointAxis
    _liveUserDimsInteracted = ReconstructionViewController._liveUserDimsInteracted
    _finishLiveUserDimsInteraction = ReconstructionViewController._finishLiveUserDimsInteraction
    _onLiveTimepointDone = ReconstructionViewController._onLiveTimepointDone
    axisStepChanged = ReconstructionViewController.axisStepChanged
    discardLiveProvisional = ReconstructionViewController.discardLiveProvisional

    def __init__(self):
        self._widget = _View()
        self._widget.controller = self
        self._widget.userDimsGestureTracking = True
        self._logger = Mock()
        self._pendingLiveResult = None
        self._liveOverlayResult = None
        self._liveOverlayCompletedCount = 0
        self._liveRenderScheduled = False
        self._liveEstablished = False
        self._liveLatestTimepoint = None
        self._liveFollowLatest = True
        self._livePinnedTimepoint = None
        self._liveProgrammaticStep = False
        self._liveUserGesturePending = False
        self._liveItem = None
        self._liveName = None
        self._liveQueueJobId = None
        self._transposeOrder = [0, 1, 2]
        self._displayedAxisLabels = ["T", "Y", "X"]
        self._axisStep = (0, 0, 0)
        self._baseAxisIndex = lambda: None
        self._rememberSliceState = lambda _result: None
        self.fullUpdate = Mock()
        self._resultsChanged = Mock()


def _result(completed=0, *, provisional=False, status="in_progress"):
    data = np.full((3, 4, 5), np.nan, np.float32)
    data[:completed] = completed
    return SimpleNamespace(
        name="scan_monalisa", _improcess_queue_job_id="job1",
        data=data, axis_labels=["T", "Y", "X"],
        metadata={
            "monalisa_streaming": {
                "provisional_preview": provisional,
                "completed_timepoints": completed,
            },
            "result_assembly": {"status": status},
        },
    )


def test_live_provisional_plane_preserves_scientific_result_and_navigation():
    ctl = _Controller()
    scientific = _result(1)
    ctl.liveResultUpdated(scientific)
    ctl._renderPendingLiveResult()
    first_entry = ctl._liveItem
    ctl._liveEstablished = True  # The new entry's selection had its first full render.

    ctl.liveTimepointProvisional(1, np.full((1, 4, 5), 8., np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._liveItem is first_entry
    assert ctl._liveItem.data(1) is scientific
    assert np.isnan(scientific.data[1]).all()
    np.testing.assert_allclose(ctl._widget.renders[-1][1], 8.)
    assert ctl._widget.steps[-1] == (0, 1)

    ctl._widget.userSelectTimepoint(0)  # the user manually browses T0
    assert not ctl._liveFollowLatest
    step_count = len(ctl._widget.steps)
    ctl.liveTimepointProvisional(1, np.full((1, 4, 5), 9., np.float32))
    ctl._renderPendingLiveResult()
    assert len(ctl._widget.steps) == step_count
    ctl._widget.resetView.assert_not_called()
    ctl.fullUpdate.assert_not_called()

    next_scientific = _result(2)
    ctl.liveResultUpdated(next_scientific)
    ctl._renderPendingLiveResult()
    assert ctl._liveItem is first_entry
    assert ctl._liveItem.data(1) is next_scientific
    np.testing.assert_allclose(ctl._widget.renders[-1][1], 2.)
    assert len(ctl._widget.steps) == step_count

    # Cancel restores the exact completed result, not the T2 overlay.
    ctl.liveTimepointProvisional(2, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    next_scientific.metadata["result_assembly"]["status"] = "cancelled"
    ctl.liveResultUpdated(next_scientific)
    ctl._renderPendingLiveResult()
    assert ctl._liveOverlayResult is None
    assert np.isnan(ctl._liveItem.data(1).data[2]).all()


def test_early_cancel_only_removes_the_unpublished_t0_entry():
    ctl = _Controller()
    transient = _result(provisional=True)
    ctl.liveResultUpdated(transient)
    ctl._renderPendingLiveResult()
    assert ctl._liveItem is not None
    ctl.discardLiveProvisional("other job")
    assert ctl._liveItem is not None
    ctl.discardLiveProvisional("job1")
    assert ctl._liveItem is None
    ctl._widget.removeData.assert_called_once_with(transient)


def test_user_slider_selection_survives_napari_data_swap_and_new_timepoints():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 1  # Follow latest initially.

    # Once T0 is selected, no later image update may change the selection.
    ctl._widget.userSelectTimepoint(0)
    assert not ctl._liveFollowLatest
    assert ctl._livePinnedTimepoint == 0
    ctl._widget.jump_on_swap = 1  # Simulate napari resetting T during repaint.
    ctl.liveTimepointProvisional(1, np.full((1, 4, 5), 2., np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 0
    assert ctl._livePinnedTimepoint == 0
    assert not ctl._liveFollowLatest

    ctl._widget.jump_on_swap = 2
    ctl.liveTimepointProvisional(2, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 0
    assert not ctl._liveFollowLatest

    # Manually selecting the currently acquiring T is still user control:
    # a later completed/new timepoint must not resume automatic following.
    ctl._widget.userSelectTimepoint(2)
    assert ctl._livePinnedTimepoint == 2
    assert not ctl._liveFollowLatest
    ctl._widget.jump_on_swap = 0
    ctl.liveResultUpdated(_result(2))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 2
    assert not ctl._liveFollowLatest


def test_full_redraw_restores_manual_timepoint_after_dims_reset():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    ctl._widget.userSelectTimepoint(0)
    ctl._liveEstablished = False  # Force the fullUpdate fallback.

    def redraw(**_kwargs):
        ctl._widget.current_t = 1
        ctl.axisStepChanged((1, 0, 0))  # napari emits during full redraw

    ctl.fullUpdate.side_effect = redraw
    ctl.liveTimepointProvisional(1, np.full((1, 4, 5), 3., np.float32))
    ctl._renderPendingLiveResult()
    assert ctl.fullUpdate.call_count == 1
    assert ctl._widget.current_t == 0
    assert ctl._livePinnedTimepoint == 0
    assert not ctl._liveFollowLatest


def test_new_live_job_resets_follow_latest_after_manual_scrubbing():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    ctl._widget.userSelectTimepoint(0)
    assert not ctl._liveFollowLatest

    newer = _result(0)
    newer._improcess_queue_job_id = "new-job"
    ctl.liveResultUpdated(newer)
    ctl._renderPendingLiveResult()
    assert ctl._liveFollowLatest
    assert ctl._livePinnedTimepoint is None


def test_other_result_selection_is_not_moved_by_streaming():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    other = _Entry(_result(1))
    ctl._widget.reconList.items.append(other)
    ctl._widget.reconList.selected = other
    ctl._widget.userSelectTimepoint(0)
    ctl._widget.steps.clear()
    ctl.liveTimepointProvisional(2, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 0
    assert ctl._widget.steps == []


def test_worker_partial_plane_protocol_and_completed_snapshot():
    class _Session:
        def __init__(self):
            self._completed = 0
            self.revision = 1
            self.preview = SimpleNamespace(
                data=np.ones((4, 5), np.float32),
                metadata={"monalisa_streaming": {"timepoint": 0}},
            )
            self.complete = _result(1)

        def preview_revision(self):
            return self.revision

        def provisional_preview(self):
            return self.preview

        def provisional_volume(self, preview):
            return _result(provisional=True)

        def result(self):
            return self.complete

    session = _Session()
    emitted = []
    worker = SimpleNamespace(
        _session=session, _enable_preview=True, _generation=5,
        _last_preview=0., _preview_interval_s=0., _last_preview_revision=None,
        _last_published_timepoint_count=0, _preview_sent=False,
        sigPreview=SimpleNamespace(emit=lambda *args: emitted.append(args)),
        _record_snapshot=lambda *args: None,
    )
    StreamProcessWorker._preview(worker, force=True)
    assert emitted[-1][1].metadata["monalisa_streaming"]["provisional_preview"]
    assert emitted[-1][2] is None
    session.revision += 1
    StreamProcessWorker._preview(worker, force=True)
    assert emitted[-1][2][0:2] == ("provisional", 0)
    session._completed = 1
    session.preview = None
    session.revision += 1
    StreamProcessWorker._preview(worker, force=True)
    assert emitted[-1][1] is session.complete
    assert emitted[-1][2] is None


def test_delayed_internal_dims_event_does_not_override_user_pin():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    ctl._widget.userSelectTimepoint(0)
    assert ctl._livePinnedTimepoint == 0
    # Emulate delivery of a late napari current_step event after the live
    # update's programmatic guard has ended. The model jumped to active T1.
    ctl._finishLiveUserDimsInteraction()
    ctl._widget.current_t = 1
    ctl.axisStepChanged((1, 0, 0))
    assert not ctl._liveFollowLatest
    assert ctl._livePinnedTimepoint == 0
    # No user intent: live render restores the pinned T0.
    ctl.liveTimepointProvisional(1, np.full((1, 4, 5), 3., np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 0
    assert not ctl._liveFollowLatest


def test_explicit_user_gesture_can_pin_the_current_live_timepoint():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    # Even clicking the CURRENT slider position is user input and disables
    # following of future timepoints.
    ctl._widget.userSelectTimepoint(1)
    ctl._finishLiveUserDimsInteraction()
    assert ctl._livePinnedTimepoint == 1
    assert not ctl._liveFollowLatest
    ctl.liveTimepointProvisional(2, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 1


def test_model_only_dims_change_does_not_disarm_auto_follow():
    ctl = _Controller()
    ctl.liveResultUpdated(_result(1))
    ctl._renderPendingLiveResult()
    ctl._liveEstablished = True
    ctl.liveTimepointProvisional(1, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    ctl._widget.current_t = 0
    ctl.axisStepChanged((0, 0, 0))  # programmatic, not a Qt gesture
    assert ctl._liveFollowLatest
    ctl.liveTimepointProvisional(2, np.ones((1, 4, 5), np.float32))
    ctl._renderPendingLiveResult()
    assert ctl._widget.current_t == 2

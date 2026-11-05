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
        self.resetView = Mock()
        self.removeData = Mock(return_value=True)

    def addNamedData(self, result, _name):
        entry = _Entry(result)
        self.reconList.items.append(entry)
        self.reconList.selected = entry
        return entry

    def tryFastLiveUpdate(self, result, _order):
        self.renders.append(np.array(result.data, copy=True))
        return True

    def moveDimStep(self, axis, step):
        self.steps.append((axis, step))

    def getCurrentItemData(self):
        entry = self.reconList.selected
        return entry.data(1) if entry is not None else None


class _Controller:
    liveResultUpdated = ReconstructionViewController.liveResultUpdated
    liveTimepointProvisional = ReconstructionViewController.liveTimepointProvisional
    _liveItemIsAlive = ReconstructionViewController._liveItemIsAlive
    _renderPendingLiveResult = ReconstructionViewController._renderPendingLiveResult
    _advanceLiveTimeSlider = ReconstructionViewController._advanceLiveTimeSlider
    _onLiveTimepointDone = ReconstructionViewController._onLiveTimepointDone
    axisStepChanged = ReconstructionViewController.axisStepChanged
    discardLiveProvisional = ReconstructionViewController.discardLiveProvisional

    def __init__(self):
        self._widget = _View()
        self._logger = Mock()
        self._pendingLiveResult = None
        self._liveOverlayResult = None
        self._liveOverlayCompletedCount = 0
        self._liveRenderScheduled = False
        self._liveEstablished = False
        self._liveLatestTimepoint = None
        self._liveFollowLatest = True
        self._liveProgrammaticStep = False
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

    ctl.axisStepChanged((0, 0, 0))  # the user manually browses T0
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

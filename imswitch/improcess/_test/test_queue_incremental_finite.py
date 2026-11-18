"""Scoped incremental sources and terminal ownership."""

from types import SimpleNamespace

import pytest

from imswitch.improcess.execution.queue_finite import (
    detector_from_logical_source, make_finite_source,
)
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement, ProcessingJob, ProcessingOrigin, ProcessingQueue,
)
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _job(mode=ExecutionRequirement.FINITE, origin=ProcessingOrigin.MANUAL):
    return ProcessingJob(
        dataset_id="lapse", label="lapse", reconstructor_id="monalisa",
        params={"keep": "snapshot"}, origin=origin,
        execution_requirement=mode, timepoint_scope=TimepointScope.range(2, 3),
        timepoint_total=5,
    )


def test_job_execution_requirement_is_independent_of_origin_and_snapshotted():
    job = _job(origin=ProcessingOrigin.AUTO)
    assert job.execution_requirement == ExecutionRequirement.FINITE
    assert job.snapshot().execution_requirement == ExecutionRequirement.FINITE
    assert job.view().execution_requirement == ExecutionRequirement.FINITE
    assert job.timepoint_scope == TimepointScope.range(2, 3)


@pytest.mark.parametrize("dataset,expected", [
    ("CAM", "CAM"), ("scan0/CAM", "CAM"),
    ("/scan42/CAM/data", "CAM"), ("/CAM/data", "CAM"),
    ("time-lapse", None), ("/scan0/CAM/other", None),
])
def test_detector_from_session_raw_reference(dataset, expected):
    source = SimpleNamespace(
        source_refs=[SimpleNamespace(dataset=dataset)], primary_dataset="fallback",
    )
    assert detector_from_logical_source(source) == expected


def test_loaded_lapse_uses_declared_anchor_detector():
    source = SimpleNamespace(sourceMetadata=SimpleNamespace(anchor_detector="CAM_B"))
    assert detector_from_logical_source(source) == "CAM_B"


def test_finite_reader_receives_snapped_scope_and_physical_origin(monkeypatch):
    import imswitch.improcess.execution.queue_finite as module

    calls = []
    monkeypatch.setattr(module, "make_incremental_disk_source", lambda path, **kwargs: (
        calls.append((path, kwargs)) or ("source", "CAM")
    ))
    source = SimpleNamespace(primary_dataset="scan12/CAM", source_refs=[])
    prepared = SimpleNamespace(
        seed_path="/tmp/rec.hdf5",
        source_type=SimpleNamespace(layout="single_lapse_file"),
    )
    assert make_finite_source(
        prepared, source, TimepointScope.range(1, 2), 4, origin=12
    ) == ("source", "CAM")
    assert calls[0][1]["timepoint_start"] == 13
    assert calls[0][1]["timepoint_stop"] == 15
    assert calls[0][1]["detector_name"] == "CAM"
    prepared.source_type.layout = "multifile_lapse"
    make_finite_source(prepared, source, TimepointScope.range(1, 2), 4, origin=12)
    assert calls[-1][1]["timepoint_start"] == 1
    assert calls[-1][1]["timepoint_stop"] == 3


class _Signal:
    def __init__(self, name, log):
        self.name = name
        self.log = log

    def emit(self, *args):
        self.log.append((self.name, *args))


def _queue_controller_stub():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    queue = ProcessingQueue()
    queue.enqueue(_job())
    queue.take_next()
    events = []
    channel = SimpleNamespace(**{
        name: _Signal(name, events)
        for name in ("sigLiveResultUpdated", "sigLiveTimepointUpdated",
                     "sigLiveTimepointDone", "sigResultFinalized",
                     "sigCurrentResultChanged", "sigResultProduced")
    })
    stub = SimpleNamespace(
        _incrementalRunner=object(), _incrementalReconstructor=object(),
        _incrementalResult=None, _incrementalError="",
        _incrementalMissingRanges=(), _incrementalPublishedResult=None,
        _incrementalCompletedTimepoints=0, _incrementalTerminalCount=None,
        _currentPlannedTimepoints=2, _currentSourceTimepoints=5,
        _skipRequested=False, queue=queue, _commChannel=channel,
        _claim_current_result=lambda result: setattr(result, "_claimed", True),
        _finish_current=lambda **kwargs: events.append(("finish", kwargs)),
        _logger=SimpleNamespace(exception=lambda *args: None),
        _emit_progress_changed=lambda: None,
    )
    from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
    from imswitch.improcess.execution.result_publication import PublicationHooks, ResultPublication
    from imswitch.improcess.model.timepoint_scope import TimepointScope
    stub._resultPublisher = ResultPublication(
        ResultAssemblyPlan.for_timepoints(
            str(queue.current.id), str(queue.current.dataset_id),
            scope=queue.current.timepoint_scope, total=5,
        ),
        PublicationHooks(
            claim=stub._claim_current_result,
            annotate_scope=lambda *args: None,
            preview=channel.sigLiveResultUpdated.emit,
            plane=channel.sigLiveTimepointUpdated.emit,
            timepoint_done=channel.sigLiveTimepointDone.emit,
            finalized=channel.sigResultFinalized.emit,
            current_changed=channel.sigCurrentResultChanged.emit,
        ),
    )
    for name in ("_on_incremental_preview", "_on_incremental_timepoint"):
        setattr(stub, name, getattr(ProcessingQueueController, name).__get__(stub))
    return stub, events


def test_incremental_preview_and_skip_preserve_published_data(monkeypatch):
    import numpy as np
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    monkeypatch.setattr(module, "annotate_result_timepoint_scope", lambda *a, **k: True)
    controller, events = _queue_controller_stub()
    result = SimpleNamespace(name="lapse", axis_labels=["T"], data=np.zeros((2, 2)))

    controller._on_incremental_preview(result, None)
    assert result._claimed
    assert result._improcess_queue_job_id == controller.queue.current.id
    assert result.metadata["incremental_completion"]["status"] == "partial"
    assert events[0] == ("sigLiveResultUpdated", result)

    controller._on_incremental_preview(None, (0, np.ones((1, 2))))
    assert events[-1][0] == "sigLiveTimepointUpdated"
    controller._on_incremental_timepoint(1)
    controller._skipRequested = True
    ProcessingQueueController._on_incremental_finished(controller, False)

    assert events[-3][0] == "sigResultFinalized"
    assert events[-3][1] is result
    assert result.metadata["result_assembly"]["status"] == "cancelled"
    assert result.metadata["result_assembly"]["published_t_ranges"] == [[2, 3]]
    assert events[-2][0] == "sigCurrentResultChanged"
    assert events[-1] == ("finish", {"skipped": True})
    assert result.metadata["incremental_completion"] == {
        "status": "partial", "completed_timepoints": 1, "requested_timepoints": 2,
    }
    assert not any(event[0] == "sigResultProduced" for event in events)


def test_skip_before_any_preview_creates_no_result():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    controller, events = _queue_controller_stub()
    controller._skipRequested = True
    ProcessingQueueController._on_incremental_finished(controller, False)
    assert events == [("finish", {"skipped": True})]


def test_completed_incremental_result_uses_existing_live_entry(monkeypatch):
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    monkeypatch.setattr(module, "annotate_result_timepoint_scope", lambda *a, **k: True)
    controller, events = _queue_controller_stub()
    preview = SimpleNamespace(name="lapse")
    final = SimpleNamespace(name="lapse")
    controller._on_incremental_preview(preview, None)
    controller._on_incremental_timepoint(2)
    controller._incrementalResult = final
    ProcessingQueueController._on_incremental_finished(controller, True)
    assert [event[0] for event in events] == [
        "sigLiveResultUpdated", "sigLiveTimepointDone", "sigLiveResultUpdated",
        "sigResultFinalized", "sigCurrentResultChanged", "finish",
    ]
    assert final._improcess_queue_job_id == preview._improcess_queue_job_id
    assert final.metadata["incremental_completion"]["status"] == "complete"
    assert events[3][1] is final
    assert events[-1] == ("finish", {})


def test_failed_incremental_run_retains_partial_without_success_label(monkeypatch):
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    monkeypatch.setattr(module, "annotate_result_timepoint_scope", lambda *a, **k: True)
    controller, events = _queue_controller_stub()
    preview = SimpleNamespace(name="lapse")
    controller._on_incremental_preview(preview, None)
    controller._incrementalError = "source failed"
    ProcessingQueueController._on_incremental_finished(controller, False)
    assert events[-1] == ("finish", {"error": "source failed"})
    assert preview.metadata["incremental_completion"]["status"] == "partial"
    assert [event[0] for event in events].count("sigResultFinalized") == 1


def test_lapse_detector_inspection_uses_selected_scan_group(tmp_path):
    import h5py
    import numpy as np
    from imswitch.improcess.execution.disk_sources import inspect_detector_names

    path = tmp_path / "two_lapses.h5"
    with h5py.File(path, "w") as file:
        for idx, detector in ((0, "CAM_A"), (12, "CAM_B")):
            group = file.create_group(f"scan{idx}").create_group(detector)
            group.create_dataset("data", data=np.ones((2, 2, 3), np.uint16))
    assert inspect_detector_names(path) == ("CAM_A",)
    assert inspect_detector_names(path, timepoint_start=12) == ("CAM_B",)

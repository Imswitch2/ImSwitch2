"""M2A: finite incremental queue policy, scoped sources and terminal ownership."""

from types import SimpleNamespace

import pytest

from imswitch.improcess.execution.queue_finite import (
    detector_from_logical_source, finite_incremental_eligible, make_finite_source,
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


def test_finite_incremental_only_for_explicit_finite_logical_lapses():
    source = SimpleNamespace(kind="time-lapse")
    plugin = SimpleNamespace(id="monalisa", supports_streaming=True, result_role="derived")
    assert finite_incremental_eligible(_job(), source, plugin, source_kind="time-lapse")
    assert finite_incremental_eligible(
        _job(origin=ProcessingOrigin.AUTO), source, plugin, source_kind="time-lapse"
    )  # Origin never selects execution mode.
    assert not finite_incremental_eligible(
        _job(ExecutionRequirement.POLICY), source, plugin, source_kind="time-lapse"
    )
    assert not finite_incremental_eligible(
        _job(), source, plugin, source_kind="scan"
    )
    assert not finite_incremental_eligible(
        _job(), source, SimpleNamespace(id="offline", supports_streaming=False),
        source_kind="time-lapse"
    )


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


def test_skip_never_publishes_provisional_result(monkeypatch):
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    queue = ProcessingQueue()
    queue.enqueue(_job())
    queue.take_next()
    outcome = []
    fake = SimpleNamespace(
        _incrementalRunner=object(), _incrementalReconstructor=None,
        _incrementalResult=SimpleNamespace(name="partial"),
        _incrementalError="", _skipRequested=True, queue=queue,
        _finish_current=lambda **kw: outcome.append(kw),
    )
    ProcessingQueueController._on_incremental_finished(fake, True)
    assert outcome == [{"skipped": True}]
    assert fake._incrementalResult is None
    assert fake._incrementalRunner is None


def test_completed_result_published_once_after_runner_has_stopped(monkeypatch):
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    queue = ProcessingQueue()
    queue.enqueue(_job())
    queue.take_next()
    result = SimpleNamespace(name="result")
    events = []
    plugin = SimpleNamespace(id="monalisa")
    monkeypatch.setattr(module, "get_registry", lambda: SimpleNamespace(
        get_reconstructor=lambda *_a, **_k: plugin
    ))
    monkeypatch.setattr(module, "annotate_result_timepoint_scope", lambda res, scope, total: (
        events.append(("scope", scope, total)) or True
    ))
    fake = SimpleNamespace(
        _incrementalRunner=object(), _incrementalReconstructor=plugin,
        _incrementalResult=result,
        _incrementalError="", _incrementalMissingRanges=(),
        _skipRequested=False, queue=queue, _currentSourceTimepoints=5,
        _claim_current_result=lambda r: events.append(("claim", r)),
        _reconstructorManager=SimpleNamespace(publishQueuedResult=lambda r, label, reconstructor: (
            events.append(("publish", r, label, reconstructor))
        )),
        _finish_current=lambda **kw: events.append(("finish", kw)),
        _logger=SimpleNamespace(exception=lambda *args: None),
    )
    ProcessingQueueController._on_incremental_finished(fake, True)
    assert [e[0] for e in events] == ["scope", "claim", "publish", "finish"]
    assert events[0][1] == TimepointScope.range(2, 3)
    assert events[0][2] == 5
    assert events[2][1] is result
    assert fake._incrementalRunner is None


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

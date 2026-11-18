"""HDF5 staged acquisition and explicit early-STOP behavior for Session.

The sidecar never makes a missing timepoint readable and never relaxes the
conservative Session Auto readiness gate. Qt tests run in the ImSwitch env.
"""
from __future__ import annotations

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

pytest.importorskip("qtpy")

from imswitch.imcommon.model.hdf5_lapse_terminal import (
    read_early_stop, write_early_stop,
)
from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController
from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
from imswitch.improcess.execution.readiness import probe_scope
from imswitch.improcess.live.discovery import DirectoryWatcher
from imswitch.improcess.model.acquisition_progress import AcquisitionProgress
from imswitch.improcess.model.processing_queue import ProcessingOrigin, ProcessingQueue
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _timepoint(path, t, *, total=5, shared=False):
    with h5py.File(path, "a", libver="latest") as root:
        group = root.create_group(f"scan{t}/CAM" if shared else "CAM")
        ds = group.create_dataset("data", data=np.ones((2, 3, 4), np.uint16))
        ds.attrs.update({
            "writing": False,
            "recording:num_timepoints": total,
            "recording:lapse_index": t,
            "recording:single_lapse_file": shared,
            "recording:planned_frames": 2,
            "recording:actual_frames": 2,
            "recording:completion_outcome": np.bytes_(b"complete"),
        })
        group.create_dataset("frames_committed", data=[2])
        group.create_dataset("stream_complete", data=[1])


@pytest.mark.parametrize("shared", [False, True])
def test_stop_marker_requires_verified_prefix(shared, tmp_path):
    anchor = tmp_path / ("rec_CAM.hdf5" if shared else "rec_scan0_CAM.hdf5")
    refs = []
    for t in range(2):
        path = anchor if shared or t == 0 else tmp_path / f"rec_scan{t}_CAM.hdf5"
        _timepoint(path, t, shared=shared)
        refs.append(SimpleNamespace(path=str(path),
                                    dataset=f"scan{t}/CAM" if shared else "CAM",
                                    timepoint_index=t))
    source = SimpleNamespace(source_refs=refs, sourceMetadata=None)
    job = SimpleNamespace(timepoint_total=5, timepoint_scope=TimepointScope.all())
    checked = probe_scope(source, job, is_lapse=True, inspected={}, activity_observed=True)
    assert checked.progress.completed_timepoints == (0, 1)
    dataset = SimpleNamespace(primary_path=str(anchor), kind="time-lapse")

    assert ProcessingSessionController._early_stop_progress(dataset, checked.progress).status == "acquiring"
    write_early_stop(anchor, completed=2, planned=5)
    stopped = ProcessingSessionController._early_stop_progress(dataset, checked.progress)
    assert stopped.status == "finished_early"
    assert ProcessingSessionController._acquisition_progress_label(stopped) == "Finished 2/5"
    assert stopped.planned_timepoints == 5

    # A sidecar is *not* a substitute for verified physical data: if one T
    # is absent, no Finished status is granted.
    missing = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0,), status="acquiring"
    )
    assert ProcessingSessionController._early_stop_progress(dataset, missing).status == "acquiring"
    merged = ProcessingSessionController._merge_acquisition_progress(
        stopped, checked.progress
    )
    assert merged.status == "finished_early"


def test_stop_marker_is_validated_against_anchor_and_total(tmp_path):
    path = tmp_path / "scan0_CAM.hdf5"
    path.write_bytes(b"sealed")
    write_early_stop(path, completed=2, planned=5)
    assert read_early_stop(path, expected_total=5) == 2
    assert read_early_stop(path, expected_total=6) is None
    path.unlink()
    assert read_early_stop(path, expected_total=5) is None


def test_session_watcher_notifies_stages_without_treating_claims_as_stores(tmp_path):
    watcher = DirectoryWatcher(str(tmp_path))
    assert watcher.scan_once() == []
    stage = tmp_path / "rec_CAM.hdf5.partial"
    stage.write_bytes(b"in progress")
    (tmp_path / "rec_CAM.hdf5.partial.claim").touch()
    found, _changed, _removed = watcher.scan_updates()
    assert found == [str(stage)]
    final = tmp_path / "rec_CAM.hdf5"
    stage.rename(final)
    marker = write_early_stop(final, completed=1, planned=5)
    found, _changed, removed = watcher.scan_updates()
    assert str(final) in found and str(marker) in found
    assert removed == [str(stage)]


def test_partial_preserves_first_arrival_auto_recipe(tmp_path):
    stage = tmp_path / "rec_CAM.hdf5.partial"
    stage.touch()
    public = tmp_path / "rec_CAM.hdf5"
    events = []
    queue = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        captureAutoRecipe=lambda: {"params": {"snapshot": "first"}},
    )
    session = SimpleNamespace(datasets=[], raw_root=tmp_path,
                              results_root=tmp_path / "results")
    fake = SimpleNamespace(
        _sessionMode="follow", _session=session,
        _watchAutoArrivals={}, _watchPendingPaths=set(),
        _watchUnresolvedPaths=set(),
        _watchRetrySchedule=SimpleNamespace(track=lambda _p: None),
        _watchReconcileTimer=SimpleNamespace(start=lambda: None),
        _syncWatchRetryTimer=lambda: None,
        _queue_controller=lambda: queue,
        _dataset_ids_for_watch_path=lambda _path: (),
        _pathBelongsToResults=lambda _path: False,
        _sessionKnowsRootEntry=lambda _path: False,
        _stagedHdf5Paths=set(),
        _collect_staged_hdf5=lambda: {str(stage)} if stage.exists() else set(),
        _render=lambda: events.append("render"),
        sigAutoAcquisitionsAdmitted=SimpleNamespace(emit=lambda *args: events.append(args)),
    )
    fake._refresh_staged_hdf5 = lambda: ProcessingSessionController._refresh_staged_hdf5(fake)
    ProcessingSessionController._sessionPathFound(fake, str(stage))
    assert fake._watchAutoArrivals[str(public)]["params"]["snapshot"] == "first"
    queue.captureAutoRecipe = lambda: {"params": {"snapshot": "late"}}
    stage.rename(public)
    ProcessingSessionController._sessionPathRemoved(fake, str(stage))
    ProcessingSessionController._sessionPathFound(fake, str(public))
    assert fake._watchAutoArrivals[str(public)]["params"]["snapshot"] == "first"
    session.datasets = [SimpleNamespace(id="dataset", primary_path=str(public), source_refs=[])]
    ProcessingSessionController._admit_resolved_arrivals(fake, [str(public)])
    assert events[-1][1]["params"]["snapshot"] == "first"


def test_manual_all_timepoints_uses_verified_stopped_prefix_and_retains_total():
    dataset = SimpleNamespace(id="lapse", label="lapse", kind="time-lapse",
                              planned_timepoints=5, raw_deleted=False)
    source = SimpleNamespace(dataset_by_id=lambda _id: dataset)
    queue = ProcessingQueue()
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="finished_early"
    )
    fake = SimpleNamespace(
        _sessionController=SimpleNamespace(
            session=source, _acquisitionProgress={"lapse": progress}),
        _main=SimpleNamespace(_activeReconstructor=SimpleNamespace(id="monalisa")),
        queue=queue,
        _snapshot_params=lambda _reconstructor: {},
        _job_for_dataset=ProcessingQueueController._job_for_dataset,
        _emit_changed=lambda: None,
        _process_next=lambda: None,
    )
    assert ProcessingQueueController.enqueueSessionDatasets(fake, ["lapse"]) == 1
    job = queue.waiting[0]
    assert job.origin == ProcessingOrigin.MANUAL
    assert job.timepoint_total == 5
    assert job.timepoint_scope.bounds(5) == (0, 2)
    assert job.timepoint_scope.to_dict(5)["count"] == 2


def test_staged_file_only_creates_a_nonreconstructable_ui_placeholder(tmp_path):
    from imswitch.improcess.model.processing_session import ProcessingSession

    stage = tmp_path / "rec_CAM.hdf5.partial"
    stage.write_bytes(b"not an HDF5 container yet")
    (tmp_path / "rec_CAM.hdf5.partial.claim").touch()
    session = ProcessingSession(tmp_path, tmp_path / "results")
    assert list(session._candidate_raw_sources()) == []
    shown = []
    fake = SimpleNamespace(
        _session=session, _stagedHdf5Paths={str(stage)},
        _selectedDatasetId=None,
        _queue_states=lambda: {},
        _widget=SimpleNamespace(setDatasets=shown.append),
        _renderSelected=lambda: None,
    )
    ProcessingSessionController._render(fake)
    assert len(shown[0]) == 1
    assert shown[0][0]["label"] == "rec_CAM.hdf5"
    assert shown[0][0]["status"] == "Acquiring"
    assert shown[0][0]["id"] is None
    assert shown[0][0]["provisional"] is True


def test_manual_request_retires_stopped_auto_job_waiting_for_missing_timepoints():
    dataset = SimpleNamespace(id="lapse", label="lapse", kind="time-lapse",
                              planned_timepoints=5, raw_deleted=False)
    session = SimpleNamespace(dataset_by_id=lambda _id: dataset)
    queue = ProcessingQueue()
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="finished_early"
    )
    retained = []
    fake = SimpleNamespace(
        _sessionController=SimpleNamespace(
            session=session, _acquisitionProgress={"lapse": progress}),
        _main=SimpleNamespace(_activeReconstructor=SimpleNamespace(id="monalisa")),
        queue=queue,
        _snapshot_params=lambda _reconstructor: {},
        _job_for_dataset=ProcessingQueueController._job_for_dataset,
        _emit_changed=lambda: None,
        _process_next=lambda: None,
        _cancel_waiting_auto_timepoints=lambda job_id: retained.append(job_id),
        _clear_readiness=lambda _job_id: None,
    )
    auto = ProcessingQueueController._job_for_dataset(
        dataset, fake._main._activeReconstructor, {},
        origin=ProcessingOrigin.AUTO, timepoint_scope=TimepointScope.all()
    )
    assert queue.enqueue(auto)
    fake._retire_waiting_auto_for_stopped_lapse = (
        lambda dataset_id:
        ProcessingQueueController._retire_waiting_auto_for_stopped_lapse(fake, dataset_id)
    )
    assert ProcessingQueueController.enqueueSessionDatasets(fake, ["lapse"]) == 1
    assert retained == [auto.id]
    manual = queue.waiting[0]
    assert manual.origin == ProcessingOrigin.MANUAL
    assert manual.timepoint_total == 5
    assert manual.timepoint_scope.bounds(5) == (0, 2)


def test_graceful_stop_counts_successfully_finalized_current_timepoint():
    from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
    from imswitch.imcontrol.model import RecMode

    observed = []
    fake = SimpleNamespace(
        recMode=RecMode.ScanLapse, lapseCurrent=2,
        _hdf5LapseSealedIndices={0, 1},
        _recordingSignalMatches=lambda generation: generation == 23,
        recordingEnded=lambda: observed.append('ended'),
    )
    RecordingController._recordingEndedDetailed(fake, 23)
    assert fake._hdf5LapseSealedIndices == {0, 1, 2}
    assert observed == ['ended']
    RecordingController._recordingEndedDetailed(fake, 22)
    assert fake._hdf5LapseSealedIndices == {0, 1, 2}

"""TIFF Session discovery and finite-only Auto readiness contracts.

OME XML is attached after the TIFF has been closed, just like the ImControl
streaming storer. No test ever needs to decode pixel values for Auto readiness.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

from imswitch.imcommon.model.ome_metadata import OmeAxis, OmeImageMeta, build_ome_xml
from imswitch.improcess.execution.auto_timepoints import probe_completed_prefix
from imswitch.improcess.execution.readiness import (
    PhysicalReference, ReadinessState, inspect_physical_reference, probe_scope,
)
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.processing_session import ProcessingSession


def _write(path, *, outcome="complete", actual=3, planned=3, lapse=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.ones((3, 32, 32), np.uint16)
    # Bare BigTIFF is what exists while an ImControl TIFF stream is active.
    tifffile.imwrite(path, data, ome=False, bigtiff=True, photometric="minisblack")
    if outcome is not None:
        _finalize(path, outcome=outcome, actual=actual, planned=planned, lapse=lapse)
    return path


def _finalize(path, *, outcome="complete", actual=3, planned=3, lapse=None, extra_attrs=None):
    attrs = {
        "recording:completion_outcome": outcome,
        "recording:planned_frames": planned,
        "recording:actual_frames": actual,
        "recording:planned_partitions": 1,
        "recording:actual_partitions": 1,
    }
    if lapse is not None:
        index, total = lapse
        attrs.update({
            "recording:num_timepoints": total,
            "recording:lapse_index": index,
            "recording:single_lapse_file": False,
            "recording:detector_name": "CAM",
        })
    attrs.update(extra_attrs or {})
    meta = OmeImageMeta(
        "CAM",
        [OmeAxis("t", "time", "s"), OmeAxis("y", "space", "µm"),
         OmeAxis("x", "space", "µm")],
        [1.0, 0.1, 0.1],
        dtype=np.uint16, annotations=attrs,
    )
    tifffile.tiffcomment(path, build_ome_xml(meta, (3, 32, 32)))


def _job(total=None):
    return SimpleNamespace(timepoint_scope=None, timepoint_total=total)


def test_tiff_snapshot_and_external_tiff_appear_in_session(tmp_path):
    raw = tmp_path / "raw"
    _write(raw / "snapshot_CAM.ome.tiff")
    _write(raw / "third_party.tif", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    assert {item.primary_path for item in session.datasets} == {
        str(raw / "snapshot_CAM.ome.tiff"), str(raw / "third_party.tif"),
    }
    for item in session.datasets:
        obj = DataObj(item.label, item.primary_dataset, path=item.primary_path)
        try:
            obj.checkAndOpenData()
            assert obj.data_source is not None
        finally:
            obj.checkAndUnloadData()


def test_completed_tiff_is_ready_but_plain_and_stopped_early_are_manual_only(tmp_path):
    completed = _write(tmp_path / "complete.ome.tiff")
    plain = _write(tmp_path / "plain.tiff", outcome=None)
    stopped = _write(tmp_path / "stopped.ome.tiff", outcome="stopped_early", actual=2)
    for path, expected in (
        (completed, ReadinessState.READY),
        (plain, ReadinessState.UNKNOWN),
        (stopped, ReadinessState.UNKNOWN),
    ):
        source = SimpleNamespace(primary_path=str(path), primary_dataset=None)
        status = probe_scope(source, _job(), is_lapse=False)
        assert status.state == expected
        assert status.live_readable is False
        assert status.verified_references == (
            (PhysicalReference(str(path), None, 0),) if expected == ReadinessState.READY else ()
        )
    assert probe_scope(
        SimpleNamespace(primary_path=str(stopped), primary_dataset=None),
        _job(), is_lapse=False,
    ).progress.status == "incomplete"


@pytest.mark.parametrize("actual,planned", [(2, 3), (4, 4), (3, 4)])
def test_tiff_declared_count_must_match_physical_pages_and_plan(tmp_path, actual, planned):
    path = _write(tmp_path / "bad.ome.tiff", actual=actual, planned=planned)
    source = SimpleNamespace(primary_path=str(path), primary_dataset=None)
    assert probe_scope(source, _job(), is_lapse=False).state == ReadinessState.UNKNOWN


def test_tiff_stable_identity_across_ome_finalization(tmp_path):
    path = _write(tmp_path / "acquiring_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(tmp_path, tmp_path / "results")
    session.reconcile_all()
    before = session.datasets[0]
    assert probe_scope(before, _job(), is_lapse=False).state == ReadinessState.UNKNOWN

    _finalize(path)
    # Auto may already be queued with the original generic series name.
    assert probe_scope(before, _job(), is_lapse=False).state == ReadinessState.READY
    session.reconcile_path(path)
    after = session.datasets[0]
    assert after.id == before.id
    assert after.source_key == before.source_key
    assert probe_scope(after, _job(), is_lapse=False).state == ReadinessState.READY


def test_tiff_timelapse_finishes_per_physical_timepoint(tmp_path):
    # ImControl's one-TIFF-per-timepoint path is already handled by lapse_source.
    raw = tmp_path / "raw"
    a = _write(raw / "exp_time0_CAM.ome.tiff", lapse=(0, 2))
    b = _write(raw / "exp_time1_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    logical = [ds for ds in session.datasets if ds.kind == "time-lapse"]
    assert len(logical) == 1
    assert len(session.datasets) == 1  # unfinished T1 is not a separate scan
    lapse = logical[0]
    assert lapse.planned_timepoints == 2
    assert {r.path for r in lapse.source_refs} == {str(a), str(b)}
    assert probe_scope(lapse, _job(2), is_lapse=True).state != ReadinessState.READY

    _finalize(b, lapse=(1, 2))
    session.reconcile_path(b)
    lapse = next(ds for ds in session.datasets if ds.kind == "time-lapse")
    readiness = probe_scope(lapse, _job(2), is_lapse=True)
    assert readiness.state == ReadinessState.READY
    assert readiness.live_readable is False
    assert readiness.progress.completed_timepoints == (0, 1)
    prefix = probe_completed_prefix(lapse, _job(2), next_t=0)
    assert prefix.indices == (0, 1)
    assert len(prefix.references) == 2


def test_new_unfinalized_tiff_lapse_member_does_not_replace_known_lapse(tmp_path):
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    raw = tmp_path / "raw"
    _write(raw / "exp_time0_CAM.ome.tiff", lapse=(0, 2))
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    (initial,) = session.datasets
    assert initial.kind == "time-lapse"

    pending = _write(raw / "exp_time1_CAM.ome.tiff", outcome=None)
    fake = SimpleNamespace(_session=session)
    assert ProcessingSessionController._is_known_lapse_member(fake, str(pending))
    assert session.reconcile_path(pending) is False
    assert len(session.datasets) == 1
    assert session.datasets[0].id == initial.id

    _finalize(pending, lapse=(1, 2))
    assert session.reconcile_path(pending)
    assert len(session.datasets) == 1
    assert session.datasets[0].id == initial.id
    assert probe_scope(session.datasets[0], _job(2), is_lapse=True).state == ReadinessState.READY


def test_tiff_discovery_does_not_accept_temp_files(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "pending.ome.tiff.partial").write_bytes(b"temporary")
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    assert session.datasets == []


def test_tiff_file_watcher_reports_final_metadata_change(tmp_path):
    from imswitch.improcess.live.discovery import DirectoryWatcher

    path = _write(tmp_path / "recording_CAM.ome.tiff", outcome=None)
    watcher = DirectoryWatcher(str(tmp_path), include_tiff_files=True)
    assert watcher.scan_once() == [str(path)]
    _finalize(path)
    found, changed, removed = watcher.scan_updates()
    assert (found, changed, removed) == ([], [str(path)], [])


def test_nested_tiff_file_change_is_visible_via_detail_watch(tmp_path):
    from imswitch.improcess.live.discovery import DirectoryWatcher
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    folder = tmp_path / "capture"
    path = _write(folder / "recording_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(tmp_path, tmp_path / "results")
    session.reconcile_all()
    watcher = DirectoryWatcher(str(tmp_path), include_tiff_files=True)
    assert watcher.scan_once() == [str(folder), str(tmp_path / "results")]

    details = {}
    fake = SimpleNamespace(
        _session=session, _directoryWatcher=SimpleNamespace(
            set_detail_paths=lambda value: details.update(value)
        ), _acquisitionProgress={},
        _watch_entry_for_source=lambda value: ProcessingSessionController._watch_entry_for_source(fake, value),
        _zarr_watch_metadata_paths=ProcessingSessionController._zarr_watch_metadata_paths,
    )
    ProcessingSessionController._update_watcher_detail_paths(fake)
    assert str(path) in details[str(folder)]
    watcher.set_detail_paths(details)
    _finalize(path)
    assert watcher.scan_updates()[1] == [str(folder)]


def test_real_imcontrol_tiff_finalization_is_auto_ready(tmp_path):
    from imswitch.improcess._test._lapse_recordings import record_point

    path = record_point(
        tmp_path / "raw", fmt="ome.tiff", detector="Camera",
        index=0, total=2, frames=3,
    )
    session = ProcessingSession(tmp_path / "raw", tmp_path / "results")
    session.reconcile_all()
    dataset = next(ds for ds in session.datasets if ds.kind == "time-lapse")
    assert dataset.primary_path == str(path)
    # A verified physical T0 is suitable for finite progressive Auto even
    # though a complete two-point time lapse must still wait for T1.
    whole = probe_scope(dataset, _job(2), is_lapse=True)
    assert whole.state != ReadinessState.READY
    prefix = probe_completed_prefix(dataset, _job(2), next_t=0)
    assert prefix.indices == (0,)
    assert len(prefix.references) == 1


def test_streaming_directory_watcher_does_not_offer_tiff_as_live_job(tmp_path):
    from imswitch.improcess.live.discovery import DirectoryWatcher

    path = _write(tmp_path / "finite.ome.tiff")
    assert DirectoryWatcher(str(tmp_path)).scan_once() == []
    assert DirectoryWatcher(str(tmp_path), include_tiff_files=True).scan_once() == [str(path)]


def _auto_arrival_harness(session, arrival_path, recipe=None):
    """Exercise the real admission boundary without running Qt workers."""
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    if recipe is None:
        recipe = {"reconstructor_id": "monalisa", "params": {"captured": "T0"},
                  "execution_requirement": "finite"}
    emitted = []
    fake = SimpleNamespace(
        _session=session,
        _watchAutoArrivals={str(arrival_path): recipe},
        sigAutoAcquisitionsAdmitted=SimpleNamespace(
            emit=lambda ids, frozen: emitted.append((ids, frozen))
        ),
        _widget=SimpleNamespace(setStatus=lambda _message: None),
    )

    def admit():
        ProcessingSessionController._admit_resolved_arrivals(fake, [str(arrival_path)])

    return fake, emitted, admit


def test_auto_tiff_t0_waits_for_final_lapse_identity_before_queue_admission(tmp_path):
    """The provisional scan ID must never become an orphaned Auto job."""
    raw = tmp_path / "raw"
    t0 = _write(raw / "movie_time0_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    (provisional,) = session.datasets
    assert provisional.kind == "scan"
    old_id = provisional.id

    fake, emitted, admit = _auto_arrival_harness(session, t0)
    admit()  # The in-progress TIFF is readable and catalogued, but not final.
    assert emitted == []
    assert str(t0) in fake._watchAutoArrivals

    _finalize(t0, lapse=(0, 3))
    assert session.reconcile_path(t0)
    (final,) = session.datasets
    assert final.kind == "time-lapse"
    assert final.id != old_id
    admit()
    assert len(emitted) == 1
    assert emitted[0][0] == (final.id,)
    assert emitted[0][1]["params"] == {"captured": "T0"}
    assert session.dataset_by_id(emitted[0][0][0]) is final
    # The queue resolves the *admitted* ID after the provisional scan was
    # replaced, so the spurious "Queued source no longer exists" path is gone.
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    queue = SimpleNamespace(_sessionController=SimpleNamespace(session=session))
    job = SimpleNamespace(source="session", dataset_id=emitted[0][0][0])
    assert ProcessingQueueController._resolve_job_source(queue, job) is final
    assert not fake._watchAutoArrivals

    t1 = _write(raw / "movie_time1_CAM.ome.tiff", outcome=None)
    assert session.reconcile_path(t1) is False  # Belongs to known lapse.
    _finalize(t1, lapse=(1, 3))
    assert session.reconcile_path(t1)
    assert session.datasets[0].id == final.id
    admit()  # A later watcher tick must not enqueue T0 twice.
    assert len(emitted) == 1


def test_auto_completed_single_tiff_keeps_scan_identity(tmp_path):
    raw = tmp_path / "raw"
    path = _write(raw / "snapshot_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    scan_id = session.datasets[0].id
    fake, emitted, admit = _auto_arrival_harness(session, path)
    admit()
    assert not emitted

    _finalize(path)
    session.reconcile_path(path)
    assert session.datasets[0].id == scan_id
    admit()
    assert emitted[0][0] == (scan_id,)
    assert not fake._watchAutoArrivals


def test_auto_nested_tiff_arrival_reconciles_before_admission(tmp_path):
    raw = tmp_path / "raw"
    folder = raw / "experiment"
    path = _write(folder / "movie_time0_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    fake, emitted, admit = _auto_arrival_harness(session, folder)
    admit()
    assert not emitted
    assert str(folder) in fake._watchAutoArrivals

    _finalize(path, lapse=(0, 2))
    session.reconcile_path(folder)
    assert session.datasets[0].kind == "time-lapse"
    admit()
    assert emitted[0][0] == (session.datasets[0].id,)
    assert str(folder) not in fake._watchAutoArrivals


def test_auto_tiff_stopped_early_is_never_admitted(tmp_path):
    raw = tmp_path / "raw"
    path = _write(raw / "stopped_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    fake, emitted, admit = _auto_arrival_harness(session, path)
    admit()
    assert not emitted

    _finalize(path, outcome="stopped_early", actual=2)
    session.reconcile_path(path)
    admit()
    assert not emitted
    assert not fake._watchAutoArrivals


def test_auto_tiff_never_admits_finalized_lapse_as_a_scan(tmp_path):
    """Reject a transient catalog snapshot that missed the final OME lapse ID."""
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    path = _write(tmp_path / "movie_time0_CAM.ome.tiff", lapse=(0, 2))
    session = ProcessingSession(tmp_path, tmp_path / "results")
    session.reconcile_all()
    (lapse,) = session.datasets
    assert lapse.kind == "time-lapse"
    assert ProcessingSessionController._tiff_auto_arrival_state(lapse) == "ready"
    provisional = SimpleNamespace(
        primary_path=lapse.primary_path, primary_dataset=lapse.primary_dataset,
        kind="scan", source_refs=lapse.source_refs,
    )
    assert ProcessingSessionController._tiff_auto_arrival_state(provisional) == "pending"


def test_auto_finalized_positioned_tiff_stays_eligible_as_scan(tmp_path):
    """A multi-position/tile run is intentionally not a temporal lapse."""
    path = _write(tmp_path / "tile_time0_CAM.ome.tiff", outcome=None)
    _finalize(
        path, lapse=(0, 2),
        extra_attrs={"Tiling:grid_x": 0},
    )
    session = ProcessingSession(tmp_path, tmp_path / "results")
    session.reconcile_all()
    (dataset,) = session.datasets
    assert dataset.kind == "scan"
    fake, emitted, admit = _auto_arrival_harness(session, path)
    admit()
    assert len(emitted) == 1
    assert emitted[0][0] == (dataset.id,)
    assert not fake._watchAutoArrivals


def test_auto_two_provisional_tiff_members_admit_only_one_lapse_job(tmp_path):
    """No duplicate Auto arrival when T0 and T1 predate T0 finalization."""
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    raw = tmp_path / "raw"
    t0 = _write(raw / "overlap_time0_CAM.ome.tiff", outcome=None)
    t1 = _write(raw / "overlap_time1_CAM.ome.tiff", outcome=None)
    session = ProcessingSession(raw, tmp_path / "results")
    session.reconcile_all()
    assert {ds.kind for ds in session.datasets} == {"scan"}

    # Both physical arrivals were seen while Auto was active, before the
    # writer supplied enough information to identify the logical lapse.
    token = {"reconstructor_id": "monalisa", "params": {"captured": "T0"}}
    emitted = []
    fake = SimpleNamespace(
        _session=session,
        _watchAutoArrivals={str(t0): token, str(t1): token},
        sigAutoAcquisitionsAdmitted=SimpleNamespace(
            emit=lambda ids, recipe: emitted.append((ids, recipe))
        ),
        _widget=SimpleNamespace(setStatus=lambda _msg: None),
    )
    ProcessingSessionController._admit_resolved_arrivals(fake, [str(t0), str(t1)])
    assert not emitted
    assert len(fake._watchAutoArrivals) == 2

    _finalize(t0, lapse=(0, 2))
    session.reconcile_all()
    (lapse,) = session.datasets
    assert lapse.kind == "time-lapse"
    ProcessingSessionController._admit_resolved_arrivals(fake, [str(t0), str(t1)])
    assert emitted == [((lapse.id,), token)]
    assert not fake._watchAutoArrivals

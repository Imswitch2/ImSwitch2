"""Focused regression coverage for Session acquisition visibility."""

from types import SimpleNamespace

import h5py
import numpy as np
import zarr

from imswitch.imcommon.model.acquisition_metadata import RecordingLifecycleMarkers
from imswitch.improcess.controller.ProcessingSessionController import (
    ProcessingSessionController,
)
from imswitch.improcess.execution.readiness import (
    PhysicalReference,
    Readiness,
    ReadinessState,
    probe_scope,
)
from imswitch.improcess.live.discovery import DirectoryWatcher
from imswitch.improcess.live.discovery_retry import UnresolvedDiscoveryRetry
from imswitch.improcess.model.acquisition_progress import AcquisitionProgress
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _write_hdf5_timepoint(path, physical_index, logical_index, total=5):
    with h5py.File(path, "a") as file:
        group = file.create_group(f"scan{physical_index}/CAM")
        data = group.create_dataset(
            "data", data=np.ones((2, 3, 3), dtype=np.uint16)
        )
        data.attrs.update({
            "writing": False,
            "recording:num_timepoints": total,
            "recording:lapse_index": logical_index,
            "recording:single_lapse_file": True,
            "recording:planned_frames": 2,
            "recording:actual_frames": 2,
            "recording:completion_outcome": np.bytes_(b"complete"),
        })
        group.create_dataset("frames_committed", data=[2])
        group.create_dataset("stream_complete", data=[1])


def _hdf5_source(path):
    return SimpleNamespace(
        sourceMetadata=None,
        source_refs=[SimpleNamespace(
            path=str(path), dataset="scan4/CAM", timepoint_index=0,
        )],
    )


def _job(total=5):
    return SimpleNamespace(
        timepoint_scope=TimepointScope.all(),
        timepoint_total=total,
    )


def test_single_file_hdf5_visibility_advances_without_catalog_refresh(tmp_path):
    path = tmp_path / "lapse.hdf5"
    source = _hdf5_source(path)
    cache = {}

    for logical_index in range(5):
        _write_hdf5_timepoint(path, physical_index=4 + logical_index,
                               logical_index=logical_index)
        result = probe_scope(
            source,
            _job(),
            is_lapse=True,
            inspected=cache,
            activity_observed=True,
        )
        assert result.progress.completed_count == logical_index + 1
        if logical_index < 4:
            assert result.progress.status == "acquiring"
        else:
            assert result.state == ReadinessState.READY
            assert result.progress.status == "complete"

    assert [
        ref.dataset for ref in result.progress.expected_references
    ] == [f"scan{index}/CAM" for index in range(4, 9)]
    assert result.progress.catalog_count == 5


def test_multi_file_hdf5_visibility_uses_logical_refs(tmp_path):
    refs = []
    for logical_index in range(3):
        path = tmp_path / f"lapse_time{logical_index + 4:02d}_CAM.hdf5"
        with h5py.File(path, "w") as file:
            group = file.create_group("CAM")
            data = group.create_dataset(
                "data", data=np.ones((2, 3, 3), dtype=np.uint16)
            )
            data.attrs.update({
                "writing": False,
                "recording:num_timepoints": 3,
                "recording:lapse_index": logical_index,
                "recording:planned_frames": 2,
                "recording:actual_frames": 2,
                "recording:completion_outcome": np.bytes_(b"complete"),
            })
            group.create_dataset("frames_committed", data=[2])
            group.create_dataset("stream_complete", data=[1])
        refs.append(SimpleNamespace(
            path=str(path), dataset="CAM", timepoint_index=logical_index,
        ))

    source = SimpleNamespace(sourceMetadata=None, source_refs=refs)
    result = probe_scope(source, _job(total=3), is_lapse=True, inspected={})

    assert result.state == ReadinessState.READY
    assert result.progress.catalog_timepoints == (0, 1, 2)
    assert result.progress.completed_timepoints == (0, 1, 2)


def test_growing_zarr_visibility_uses_committed_frames(tmp_path):
    path = tmp_path / "recording.zarr"
    root = zarr.open_group(str(path), mode="w")
    data = root.create_array("CAM", shape=(3, 3, 3), chunks=(1, 3, 3), dtype="u2")
    data.attrs.update({
        "writing": True,
        "recording:frames_committed": 1,
        "recording:planned_frames": 3,
        "recording:actual_frames": 1,
    })
    source = SimpleNamespace(
        sourceMetadata=None,
        source_refs=[SimpleNamespace(
            path=str(path), dataset="CAM", timepoint_index=0,
        )],
    )
    result = probe_scope(
        source,
        SimpleNamespace(timepoint_scope=TimepointScope.all(), timepoint_total=None),
        is_lapse=False,
        inspected={},
        activity_observed=True,
    )

    assert result.progress.status == "acquiring"
    assert result.progress.catalog_timepoints == (0,)

    data.attrs.update({
        "writing": False,
        "recording:frames_committed": 3,
        "recording:actual_frames": 3,
        "recording:completion_outcome": "complete",
    })
    result = probe_scope(
        source,
        SimpleNamespace(timepoint_scope=TimepointScope.all(), timepoint_total=None),
        is_lapse=False,
        inspected={},
    )

    assert result.state == ReadinessState.READY
    assert result.progress.status == "complete"
    assert result.progress.completed_timepoints == (0,)


def test_historical_inconclusive_metadata_keeps_existing_status_semantics(tmp_path):
    path = tmp_path / "legacy.hdf5"
    with h5py.File(path, "w") as file:
        group = file.create_group("scan4/CAM")
        group.create_dataset("data", data=np.ones((2, 3, 3), dtype=np.uint16))
    source = _hdf5_source(path)
    result = probe_scope(source, _job(), is_lapse=True, inspected={})

    assert result.progress.status == "unknown"
    assert result.progress.catalog_timepoints == (0,)
    assert result.progress.completed_count == 0


def test_watcher_detects_growing_hdf5_file(tmp_path):
    path = tmp_path / "recording.hdf5"
    path.write_bytes(b"initial")
    watcher = DirectoryWatcher(str(tmp_path))
    watcher.scan_once()
    path.write_bytes(b"grew")

    _new, changed, _removed = watcher.scan_updates()

    assert changed == [str(path)]


def test_watcher_detects_selected_nested_zarr_metadata(tmp_path):
    store = tmp_path / "recording.zarr"
    metadata = store / "CAM" / "data" / "zarr.json"
    metadata.parent.mkdir(parents=True)
    watcher = DirectoryWatcher(str(tmp_path))
    watcher.scan_once()
    watcher.set_detail_paths({str(store): (str(metadata),)})
    metadata.write_text("{}", encoding="utf-8")

    _new, changed, _removed = watcher.scan_updates()

    assert changed == [str(store)]


def _render_fake(progress, *, queue_state=None, queue_progress=None, recon_entries=None):
    dataset = SimpleNamespace(
        id="dataset",
        label="Recording",
        kind="time-lapse",
        planned_timepoints=5,
        raw_deleted=False,
        saved_reconstructions=[],
        source_refs=[],
        primary_path="/tmp/recording.hdf5",
        primary_dataset="scan4/CAM",
        dimension_tag="XYT",
        spatial_display_label="2D",
        dimension_filter_label="XYT",
        decoded_bytes=None,
        timepoint_origin=4,
        favorite=False,
        auto_process_ignored=False,
    )
    fake = SimpleNamespace(
        _loadingDatasetId=None,
        _currentDatasetId=None,
        _selectedDatasetId=None,
        _acquisitionProgress={"dataset": progress} if progress else {},
        _queueProgressByDatasetId={"dataset": queue_progress or {}}
        if queue_progress else {},
        _reconstruction_entries=lambda _dataset: list(recon_entries or []),
        _dataset_info=ProcessingSessionController._dataset_info,
        _dataset_source_tooltip=lambda _dataset: "Path: /tmp/recording.hdf5",
        _dataset_search_text=lambda _dataset: "Recording",
        _dataset_timestamp=lambda _dataset: None,
        _queue_progress_label=ProcessingSessionController._queue_progress_label,
        _acquisition_progress_label=ProcessingSessionController._acquisition_progress_label,
        _acquisition_tooltip=ProcessingSessionController._acquisition_tooltip,
        _processing_tooltip=ProcessingSessionController._processing_tooltip,
    )
    return ProcessingSessionController._dataset_entry(
        fake, dataset, queue_state=queue_state
    )


def test_running_reconstruction_has_priority_over_acquisition_status():
    progress = AcquisitionProgress(
        planned_timepoints=5,
        scope=(0, 5),
        catalog_timepoints=(0, 1),
        completed_timepoints=(0, 1),
        status="acquiring",
        reason="Observed source change",
    )

    entry = _render_fake(progress, queue_state="running",
                          queue_progress={"status": "Timepoint 2/5"})

    assert entry["status"] == "▶ Processing… 2/5"
    assert entry["state"] == "processing"
    assert "T=2/5" in entry["info"]
    assert "Acquisition: 2/5 timepoints complete" in entry["tooltip"]
    assert "Processing: Active reconstruction" in entry["tooltip"]


def test_acquisition_snapshot_survives_unknown_probe_and_keeps_count():
    previous = AcquisitionProgress(
        planned_timepoints=5,
        scope=(0, 5),
        catalog_timepoints=(0, 1),
        completed_timepoints=(0, 1),
        status="acquiring",
        reason="Observed acquisition activity",
    )
    current = AcquisitionProgress(
        planned_timepoints=5,
        scope=(0, 5),
        catalog_timepoints=(0,),
        completed_timepoints=(0,),
        status="unknown",
        reason="Waiting for safe source access: OSError",
    )

    merged = ProcessingSessionController._merge_acquisition_progress(
        previous, current
    )

    assert merged.status == "acquiring"
    assert merged.completed_timepoints == (0, 1)
    assert merged.catalog_timepoints == (0, 1)


def test_explicit_incomplete_acquisition_remains_visible():
    progress = AcquisitionProgress(
        planned_timepoints=5,
        scope=(0, 5),
        catalog_timepoints=(0, 1, 2),
        completed_timepoints=(0, 1, 2),
        status="incomplete",
        reason="Recording stopped early",
    )

    entry = _render_fake(progress)

    assert entry["status"] == "Incomplete 3/5"
    assert "T=3/5" in entry["info"]
    assert "Recording stopped early" in entry["tooltip"]


def test_queue_status_takes_priority_over_incomplete_acquisition():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="incomplete",
    )
    entry = _render_fake(progress, queue_state="waiting")
    assert entry["status"] == "… Queued"
    assert entry["state"] == "queued"
    assert "T=2/5" in entry["info"]


def test_inconclusive_acquisition_does_not_replace_existing_raw_label():
    progress = AcquisitionProgress(
        planned_timepoints=5,
        scope=(0, 5),
        catalog_timepoints=(0,),
        completed_timepoints=(0,),
        status="unknown",
        reason="Missing lifecycle markers",
    )

    entry = _render_fake(progress)

    assert entry["status"] == "— No result"
    assert "Acquisition: 1/5 timepoints complete" in entry["tooltip"]
    assert "Missing lifecycle markers" in entry["tooltip"]


def test_hdf5_probe_retry_path_is_reused_after_access_contention():
    path = "/tmp/recording.hdf5"
    dataset = SimpleNamespace(
        id="dataset",
        primary_path=path,
        source_refs=[],
    )
    progress = AcquisitionProgress(
        planned_timepoints=1,
        scope=(0, 1),
        status="unknown",
        reason="Waiting for safe source access: RawHdf5AccessConflict",
    )
    readiness = Readiness(
        ReadinessState.UNKNOWN,
        progress.reason,
        progress=progress,
    )
    retried = []
    fake = SimpleNamespace(
        _acquisitionProbeGeneration=0,
        _session=SimpleNamespace(dataset_by_id=lambda _id: dataset),
        _acquisitionProgress={},
        _merge_acquisition_progress=ProcessingSessionController._merge_acquisition_progress,
        _source_paths=ProcessingSessionController._source_paths,
        _watchAcquisitionRetryPaths=set(),
        _watchRetrySchedule=UnresolvedDiscoveryRetry(),
        _syncWatchRetryTimer=lambda: None,
        _update_watcher_detail_paths=lambda: None,
        _queue_states=lambda: {},
        _selectedDatasetId=None,
        _renderDataset=lambda *args, **kwargs: retried.append((args, kwargs)),
        _logger=SimpleNamespace(warning=lambda *args: None),
    )

    ProcessingSessionController._acquisition_probe_finished(
        fake,
        [("dataset", readiness, (), True, "")],
        0,
    )

    assert path in fake._watchAcquisitionRetryPaths
    assert fake._watchRetrySchedule


def test_confirmed_early_stop_shows_actual_timepoints_and_no_result():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        catalog_timepoints=(0, 1), completed_timepoints=(0, 1),
        status="finished_early", reason="Recording stopped before all planned timepoints",
    )
    entry = _render_fake(progress)
    assert entry["info"].startswith("2D · T=2")
    assert "T=2/5" not in entry["info"]
    assert entry["status"] == "— No result"
    assert entry["state"] == "raw"
    assert entry["planned_timepoints"] == 5
    assert "Planned timepoints: 5" in entry["info_tooltip"]
    assert "Verified complete timepoints: 2" in entry["info_tooltip"]
    assert "Acquisition stopped early (user STOP)" in entry["tooltip"]
    assert "Available timepoints: T0–T1" in entry["tooltip"]


def test_confirmed_early_stop_result_is_visible_without_losing_provenance():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="finished_early",
    )
    entries = [{"loaded": object()}]
    entry = _render_fake(progress, recon_entries=entries)
    assert entry["status"] == "✓ Result (1)"
    assert entry["state"] == "reconstructed"
    assert entry["info"].startswith("2D · T=2")
    assert entry["result_count"] == 1
    assert entry["planned_timepoints"] == 5
    assert "Acquisition stopped early" in entry["tooltip"]
    # The same verified progress snapshot can be presented after a Session
    # reopen; the view doesn't depend on a transient recording controller.
    restored = _render_fake(progress, recon_entries=[{"loaded": None}])
    assert restored["status"] == "✓ Result (1)"
    assert restored["info"].startswith("2D · T=2")


def test_in_progress_result_is_not_hidden_by_acquiring_label():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="acquiring",
    )
    entry = _render_fake(progress, recon_entries=[{"loaded": object()}])
    assert entry["status"] == "✓ Result (1)"
    assert entry["state"] == "reconstructed"
    assert "T=2/5" in entry["info"]
    assert "Acquisition: 2/5 timepoints complete" in entry["tooltip"]


def test_unconfirmed_prefix_keeps_planned_total_and_never_claims_stop():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status="acquiring",
        reason="Waiting for next timepoint",
    )
    entry = _render_fake(progress)
    assert entry["info"].startswith("2D · T=2/5")
    assert entry["status"] == "Acquiring 2/5"
    assert "stopped early" not in entry["tooltip"].lower()
    assert entry["planned_timepoints"] == 5


def test_completed_lapse_uses_verified_count_and_keeps_normal_result_status():
    progress = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1, 2, 3, 4), status="complete",
    )
    entry = _render_fake(progress, recon_entries=[{"loaded": object()}])
    assert entry["info"].startswith("2D · T=5")
    assert entry["status"] == "✓ Result (1)"
    assert "stopped early" not in entry["tooltip"].lower()

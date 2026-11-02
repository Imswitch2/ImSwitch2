from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.processing_session import ProcessingSession, SessionDataset
from imswitch.improcess.model.provenance import PROVENANCE_KEY, record_reconstruction
from imswitch.improcess.model.saved_result import load_saved_result, read_saved_result_metadata


def _h5_image(path: Path, dataset="Camera", *, value=1, attrs=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        image = handle.create_dataset(dataset, data=np.full((2, 4, 5), value, np.uint16))
        image.attrs["axes"] = ["Z", "Y", "X"]
        for key, item in (attrs or {}).items():
            image.attrs[key] = item
    return path


def _multi_file_lapse(root: Path, count=3):
    paths = []
    for index in range(count):
        attrs = {
            "recording:num_timepoints": count,
            "recording:lapse_index": index,
            "recording:single_lapse_file": False,
            "recording:detector_name": "Camera",
            "recording:lapse_interval_s": 5.0,
        }
        paths.append(_h5_image(root / f"exp_time{index}_Camera.h5", attrs=attrs, value=index + 1))
    return paths


def _single_file_lapse(path: Path, count=3):
    with h5py.File(path, "w") as handle:
        for index in range(count):
            detector = handle.require_group(f"scan{index}/Camera")
            image = detector.create_dataset("data", data=np.full((2, 4, 5), index + 1, np.uint16))
            image.attrs.update({
                "recording:num_timepoints": count,
                "recording:lapse_index": index,
                "recording:single_lapse_file": True,
                "recording:detector_name": "Camera",
                "recording:lapse_interval_s": 5.0,
                "axes": ["Z", "Y", "X"],
            })
    return path


class _Recon:
    id = "test-recon"
    name = "Test reconstruction"
    version = "1"
    params_version = 1


class _MonalisaLikeRecon:
    id = "monalisa"
    name = "MoNaLISA"
    version = "1"
    params_version = 4


class _InspectionRecon:
    id = "inspection-test"
    name = "Inspection test"
    version = "1"
    params_version = 1
    result_role = "inspection"


def _saved_reconstruction(raw_path: Path, results: Path, name="full", *, recon=None, params=None) -> Path:
    raw = DataObj(raw_path.name, "Camera", path=str(raw_path))
    raw.checkAndOpenData()
    result = ArrayProcessingResult(
        name,
        np.ones((4, 5), dtype=np.float32),
        ["Y", "X"],
        axis_scales=[0.1, 0.1],
        scale_unit="um",
    )
    record_reconstruction(result, recon or _Recon(), dict(params or {}), raw)
    target = results / f"{name}.h5"
    result.save(target, "hdf5")
    return target


def test_discovers_single_hdf5_scan_without_loading_pixels(tmp_path):
    raw = tmp_path / "raw"
    _h5_image(raw / "scan01.h5")

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()

    assert len(session.datasets) == 1
    dataset = session.datasets[0]
    assert dataset.kind == "scan"
    assert dataset.label == "scan01.h5"
    assert dataset.saved_reconstructions == []


def test_discovers_multifile_timelapse_as_one_dataset(tmp_path):
    raw = tmp_path / "raw"
    paths = _multi_file_lapse(raw, 3)

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()

    assert len(session.datasets) == 1
    dataset = session.datasets[0]
    assert dataset.kind == "time-lapse"
    assert dataset.planned_timepoints == 3
    assert {Path(ref.path) for ref in dataset.source_refs} == set(paths)


def test_discovers_single_file_timelapse_as_one_dataset(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    path = _single_file_lapse(raw / "lapse_Camera.h5", 3)

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()

    assert len(session.datasets) == 1
    dataset = session.datasets[0]
    assert dataset.kind == "time-lapse"
    assert dataset.planned_timepoints == 3
    assert all(Path(ref.path) == path for ref in dataset.source_refs)


def test_saved_result_is_associated_by_provenance_and_persists(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    saved = _saved_reconstruction(raw_path, results)

    session = ProcessingSession(raw_root, results)
    session.refresh()

    dataset = session.datasets[0]
    assert [Path(item.path) for item in dataset.saved_reconstructions] == [saved]
    assert dataset.status == "reconstructed"

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()
    assert reopened.datasets[0].status == "reconstructed"
    assert [Path(item.path) for item in reopened.datasets[0].saved_reconstructions] == [saved]


def test_inspection_result_is_not_a_session_processed_result(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    _saved_reconstruction(raw_path, results, recon=_InspectionRecon())

    session = ProcessingSession(raw_root, results)
    session.refresh()

    assert len(session.datasets) == 1
    assert session.datasets[0].status == "raw"
    assert session.datasets[0].saved_reconstructions == []


def test_session_sources_have_relative_identity(tmp_path):
    raw_root = tmp_path / "raw"
    raw_path = _h5_image(raw_root / "scan01.h5")

    session = ProcessingSession(raw_root, tmp_path / "results")
    session.refresh()

    dataset = session.datasets[0]
    assert dataset.source_key == "scan|scan01.h5|Camera"
    assert dataset.source_refs[0].relative_path == "scan01.h5"
    assert Path(dataset.source_refs[0].path) == raw_path


def test_favorite_can_restore_from_relative_source_identity(tmp_path):
    raw_root = tmp_path / "raw"
    _h5_image(raw_root / "scan01.h5")
    results = tmp_path / "results"

    session = ProcessingSession(raw_root, results)
    session.refresh()
    dataset = session.datasets[0]
    session.set_favorite([dataset.id], True)

    payload = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    payload["datasets"][0]["id"] = "legacy-or-moved-id"
    session.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()
    assert reopened.datasets[0].favorite is True


def test_reconcile_path_adds_new_dataset_without_losing_curation(tmp_path):
    raw_root = tmp_path / "raw"
    first_path = _h5_image(raw_root / "first.h5")
    session = ProcessingSession(raw_root, tmp_path / "results")
    session.refresh()
    first = session.datasets[0]
    session.set_favorite([first.id], True)

    second_path = _h5_image(raw_root / "second.h5")
    assert session.reconcile_path(second_path) is True

    assert {dataset.label for dataset in session.datasets} == {"first.h5", "second.h5"}
    refreshed_first = next(dataset for dataset in session.datasets if dataset.label == "first.h5")
    assert refreshed_first.favorite is True
    assert Path(refreshed_first.primary_path) == first_path


def test_legacy_final_metadata_is_ignored_and_removed_on_refresh(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    _saved_reconstruction(raw_path, results)

    session = ProcessingSession(raw_root, results)
    session.refresh()
    payload = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    payload["schema"] = 2
    payload["datasets"][0]["saved_reconstructions"][0]["final"] = True
    session.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()

    assert reopened.datasets[0].status == "reconstructed"
    assert len(reopened.datasets[0].saved_reconstructions) == 1
    migrated = json.loads(reopened.manifest_path.read_text(encoding="utf-8"))
    assert migrated["schema"] == 5
    assert "final" not in migrated["datasets"][0]["saved_reconstructions"][0]


def test_saved_result_restores_monalisa_calibration_metadata(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    raw = DataObj(raw_path.name, "Camera", path=str(raw_path))
    raw.checkAndOpenData()
    result = ArrayProcessingResult(
        "monalisa-result", np.ones((4, 5), dtype=np.float32), ["Y", "X"]
    )
    record_reconstruction(result, _MonalisaLikeRecon(), {"preset": "Fast"}, raw)
    snapshot = {
        "detector": "Camera",
        "resolved_localization": {
            "kind": "manual_grid",
            "period_x": 10.0,
            "period_y": 10.0,
            "offset_x": 1.0,
            "offset_y": 2.0,
            "stagger_axis": None,
            "stagger": 0.0,
            "source": "auto",
            "dataset": "Camera",
            "provenance": {},
        },
        "resolved_orientation": "+x+y",
    }
    result.metadata["monalisa_calibration"] = snapshot
    saved = results / "monalisa-result.h5"
    result.save(saved, "hdf5")

    assert read_saved_result_metadata(saved)["monalisa_calibration"] == snapshot
    restored = load_saved_result(saved)
    assert restored.metadata["monalisa_calibration"] == snapshot


def test_restore_saved_hdf5_result_is_lazy_and_keeps_provenance(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    saved = _saved_reconstruction(raw_path, results)
    # Coordinate datasets are legal companions of a saved time-series result;
    # the image loader must still choose the canonical ``data`` dataset.
    with h5py.File(saved, "a") as handle:
        handle.create_dataset("t_seconds", data=np.arange(3, dtype=float))

    restored = load_saved_result(saved, session_origin="session-A")

    assert restored.data.shape == (4, 5)
    assert not isinstance(restored.data, np.ndarray)
    assert restored.metadata.get(PROVENANCE_KEY) is not None
    assert restored.name == "full"
    assert Path(restored._session_saved_path) == saved.resolve()
    assert restored._session_origin == "session-A"


def test_zarr_single_scan_and_saved_result(tmp_path):
    zarr = pytest.importorskip("zarr")
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw_path = raw_root / "scan01.zarr"
    root = zarr.open_group(str(raw_path), mode="w")
    if hasattr(root, "create_array"):
        arr = root.create_array("Camera", data=np.ones((2, 4, 5), dtype=np.uint16))
    else:
        arr = root.create_dataset("Camera", data=np.ones((2, 4, 5), dtype=np.uint16))
    arr.attrs["axes"] = ["Z", "Y", "X"]

    session = ProcessingSession(raw_root, tmp_path / "results")
    session.refresh()

    assert len(session.datasets) == 1
    assert session.datasets[0].kind == "scan"


def test_zarr_multifile_timelapse_is_grouped(tmp_path):
    zarr = pytest.importorskip("zarr")
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    for index in range(3):
        path = raw_root / f"exp_time{index}_Camera.zarr"
        root = zarr.open_group(str(path), mode="w")
        if hasattr(root, "create_array"):
            arr = root.create_array("Camera", data=np.ones((2, 4, 5), dtype=np.uint16))
        else:
            arr = root.create_dataset("Camera", data=np.ones((2, 4, 5), dtype=np.uint16))
        arr.attrs.update({
            "recording:num_timepoints": 3,
            "recording:lapse_index": index,
            "recording:single_lapse_file": False,
            "recording:detector_name": "Camera",
            "recording:lapse_interval_s": 5.0,
            "axes": ["Z", "Y", "X"],
        })

    session = ProcessingSession(raw_root, tmp_path / "results")
    session.refresh()

    assert len(session.datasets) == 1
    assert session.datasets[0].kind == "time-lapse"
    assert session.datasets[0].planned_timepoints == 3


def test_session_autosave_stem_avoids_duplicating_long_raw_name():
    from imswitch.improcess.controller.ProcessingSessionController import (
        ProcessingSessionController,
    )

    class Dataset:
        primary_path = (
            "D:/data/13h03m56s_2D-850_conf_ON0.5ms30mW_RO1ms50mW_"
            "2D_xy35nm_rec_CAM.hdf5"
        )

    class Result:
        name = (
            "13h03m56s_2D-850_conf_ON0.5ms30mW_RO1ms50mW_"
            "2D_xy35nm_rec_CAM.hdf5_monalisa"
        )

    stem = ProcessingSessionController._autosave_stem(
        Dataset(), Result(), Result.name
    )

    assert stem.endswith("_monalisa")
    assert len(stem) <= 55
    assert stem.count("13h03m56s") == 1


def test_multiple_saved_results_remain_simple_reconstruction_collection(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    first = _saved_reconstruction(raw_path, results, "full")
    second = _saved_reconstruction(raw_path, results, "full_test")

    session = ProcessingSession(raw_root, results)
    session.refresh()
    dataset = session.datasets[0]

    assert dataset.status == "reconstructed"
    assert {item.name for item in dataset.saved_reconstructions} == {"full", "full_test"}
    assert {Path(item.path) for item in dataset.saved_reconstructions} == {first, second}

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()
    assert reopened.datasets[0].status == "reconstructed"
    assert {item.name for item in reopened.datasets[0].saved_reconstructions} == {
        "full", "full_test"
    }

def test_trash_reconstruction_hides_it_from_session_but_preserves_file(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    first = _saved_reconstruction(raw_path, results, "full")
    second = _saved_reconstruction(raw_path, results, "test")

    session = ProcessingSession(raw_root, results)
    session.refresh()
    dataset = session.datasets[0]
    assert dataset.status == "reconstructed"

    trashed = session.trash_reconstruction(dataset.id, second)
    assert trashed is not None
    assert trashed.exists()
    assert ".trash" in trashed.parts
    assert not second.exists()

    session.refresh()
    dataset = session.datasets[0]
    assert dataset.status == "reconstructed"
    assert [Path(item.path) for item in dataset.saved_reconstructions] == [first]


def test_session_reconstruction_name_uses_preset_and_tooltip_uses_provenance(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    _saved_reconstruction(
        raw_path,
        results,
        "scan01_recon",
        recon=_MonalisaLikeRecon(),
        params={
            "preset": "Fast",
            "spot_fwhm": 210.0,
            "spot_fwhm_unit": "nm",
            "reassignment": "off",
            "intensity_correction": False,
            "cell_offsets": False,
            "localization_mode": "Auto",
        },
    )

    session = ProcessingSession(raw_root, results)
    session.refresh()
    saved = session.datasets[0].saved_reconstructions[0]

    assert saved.name == "MoNaLISA - Fast"
    assert saved.display_name == "scan01_recon"
    assert "FWHM: 210 nm" in saved.tooltip
    assert "Reassignment: off" in saved.tooltip
    assert "Localization: Auto" in saved.tooltip


def test_results_subfolder_is_excluded_from_raw_discovery(tmp_path):
    raw_root = tmp_path / "raw"
    results = raw_root / "results"
    _h5_image(raw_root / "scan01.h5")
    _h5_image(results / "looks_like_raw.h5")

    session = ProcessingSession(raw_root, results)
    session.refresh()

    assert [dataset.label for dataset in session.datasets] == ["scan01.h5"]


def test_deleted_raw_dataset_can_remain_as_session_tombstone(tmp_path):
    raw_root = tmp_path / "raw"
    results = raw_root / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    saved = _saved_reconstruction(raw_path, results, "full")

    session = ProcessingSession(raw_root, results)
    session.refresh()
    dataset_id = session.datasets[0].id
    session.mark_raw_deleted([dataset_id])
    raw_path.unlink()

    session.refresh()

    assert len(session.datasets) == 1
    dataset = session.datasets[0]
    assert dataset.id == dataset_id
    assert dataset.raw_deleted is True
    assert dataset.status == "reconstructed"
    assert [Path(item.path) for item in dataset.saved_reconstructions] == [saved]


def test_source_metadata_reports_decoded_size_without_materializing(tmp_path):
    raw_path = _h5_image(tmp_path / "scan01.h5")

    _scan_label, decoded = ProcessingSession._source_metadata(raw_path, "Camera")

    assert decoded == 2 * 4 * 5 * np.dtype(np.uint16).itemsize


def test_scan_labels_use_canonical_axis_tags():
    def resolved(*kinds):
        loops = [SimpleNamespace(kind=kind, count=2) for kind in kinds]
        return SimpleNamespace(layout=SimpleNamespace(event_loops=loops))

    assert ProcessingSession._scan_label_from_layout(resolved("scan_x", "scan_y")) == "XY"
    assert ProcessingSession._scan_label_from_layout(resolved("scan_y", "scan_z")) == "YZ"
    assert ProcessingSession._scan_label_from_layout(
        resolved("scan_x", "scan_y", "scan_z")
    ) == "XYZ"


def test_dimension_tag_combines_spatial_axes_and_time():
    common = dict(
        id="dataset",
        label="dataset",
        primary_path="/tmp/data.h5",
        primary_dataset="Camera",
    )

    assert SessionDataset(kind="scan", scan_label="XY", **common).dimension_tag == "XY"
    assert (
        SessionDataset(
            kind="time-lapse", scan_label="XY", planned_timepoints=10, **common
        ).dimension_tag
        == "XYT"
    )
    # A nominal time-lapse with no actual temporal multiplicity stays spatial.
    assert (
        SessionDataset(
            kind="time-lapse", scan_label="XY", planned_timepoints=1, **common
        ).dimension_tag
        == "XY"
    )
    # Old Session manifests used the presentation labels 2D / 3D. Keep those
    # readable so deleted-data tombstones still participate in filtering.
    assert SessionDataset(kind="scan", scan_label="2D", **common).dimension_tag == "XY"
    assert (
        SessionDataset(
            kind="time-lapse", scan_label="3D", planned_timepoints=8, **common
        ).dimension_tag
        == "XYZT"
    )


def test_dimension_display_aliases_are_presentation_only():
    common = dict(
        id="dataset",
        label="dataset",
        primary_path="/tmp/data.h5",
        primary_dataset="Camera",
    )

    xy = SessionDataset(kind="scan", scan_label="XY", **common)
    xyt = SessionDataset(
        kind="time-lapse", scan_label="XY", planned_timepoints=10, **common
    )
    xyz = SessionDataset(kind="scan", scan_label="XYZ", **common)
    yzt = SessionDataset(
        kind="time-lapse", scan_label="YZ", planned_timepoints=10, **common
    )

    assert xy.spatial_display_label == "2D"
    assert xy.dimension_filter_label == "XY (2D)"
    assert xyt.spatial_display_label == "2D"
    assert xyt.dimension_filter_label == "XYT (2D)"
    assert xyz.spatial_display_label == "3D"
    assert xyz.dimension_filter_label == "XYZ (3D)"
    assert yzt.spatial_display_label == "YZ"
    assert yzt.dimension_filter_label == "YZT"


def _recorded_result_for_source(path: Path, dataset_name: str | None, name: str):
    """Small provenance-bearing reconstruction result for Session matching tests."""
    source = DataObj(path.name, dataset_name, path=str(path))
    source.checkAndOpenData()
    try:
        result = ArrayProcessingResult(
            name,
            np.ones((4, 5), dtype=np.float32),
            ["Y", "X"],
            axis_scales=[0.1, 0.1],
            scale_unit="um",
        )
        record_reconstruction(result, _Recon(), {}, source)
        return result
    finally:
        source.checkAndUnloadData()


def _consolidated_result(results, name="multi"):
    from imswitch.improcess.model.provenance import record_consolidation

    merged = ArrayProcessingResult(
        name,
        np.ones((len(results), 4, 5), dtype=np.float32),
        ["T", "Y", "X"],
        axis_scales=[1.0, 0.1, 0.1],
        scale_unit="um",
    )
    record_consolidation(merged, results, _Recon())
    return merged


def _consolidated_for_session_dataset(dataset):
    pieces = []
    for index, ref in enumerate(dataset.source_refs):
        dataset_name = ref.dataset
        if dataset_name is None:
            names = list(DataObj.getDatasetNames(ref.path))
            assert len(names) == 1
            dataset_name = names[0]
        pieces.append(
            _recorded_result_for_source(Path(ref.path), dataset_name, f"t{index}")
        )
    return _consolidated_result(pieces)


def test_multifile_timelapse_consolidation_resolves_to_one_session_dataset(tmp_path):
    raw = tmp_path / "raw"
    _multi_file_lapse(raw, 3)

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    dataset = session.datasets[0]
    merged = _consolidated_for_session_dataset(dataset)

    assert dataset.kind == "time-lapse"
    assert session.dataset_for_result(merged).id == dataset.id


def test_single_file_timelapse_consolidation_resolves_to_one_session_dataset(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    _single_file_lapse(raw / "lapse_Camera.h5", 3)

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    dataset = session.datasets[0]
    merged = _consolidated_for_session_dataset(dataset)

    assert dataset.kind == "time-lapse"
    assert session.dataset_for_result(merged).id == dataset.id


def test_consolidation_across_unrelated_session_datasets_is_not_session_owned(tmp_path):
    raw = tmp_path / "raw"
    first_path = _h5_image(raw / "scan_a.h5")
    second_path = _h5_image(raw / "scan_b.h5")

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    merged = _consolidated_result([
        _recorded_result_for_source(first_path, "Camera", "a"),
        _recorded_result_for_source(second_path, "Camera", "b"),
    ])

    assert len(session.datasets) == 2
    assert session.dataset_for_result(merged) is None


def test_consolidation_with_source_outside_session_is_not_session_owned(tmp_path):
    raw = tmp_path / "raw"
    in_session = _h5_image(raw / "scan_a.h5")
    outside = _h5_image(tmp_path / "outside" / "scan_external.h5")

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    merged = _consolidated_result([
        _recorded_result_for_source(in_session, "Camera", "inside"),
        _recorded_result_for_source(outside, "Camera", "outside"),
    ])

    assert len(session.datasets) == 1
    assert session.dataset_for_result(merged) is None


def test_saved_timelapse_consolidation_is_discovered_under_logical_lapse(tmp_path):
    raw = tmp_path / "raw"
    results = tmp_path / "results"
    _multi_file_lapse(raw, 3)

    session = ProcessingSession(raw, results)
    session.refresh()
    dataset = session.datasets[0]
    merged = _consolidated_for_session_dataset(dataset)
    saved = results / "lapse_multi.h5"
    merged.save(saved, "hdf5")

    session.refresh()
    dataset = session.datasets[0]

    assert [Path(item.path) for item in dataset.saved_reconstructions] == [saved]


def test_session_controller_recognizes_consolidation_and_resolves_multidata_source(tmp_path):
    from imswitch.improcess.controller.ProcessingSessionController import (
        ProcessingSessionController,
    )

    path = _h5_image(tmp_path / "scan.h5")
    first = _recorded_result_for_source(path, "Camera", "first")
    second = _recorded_result_for_source(path, "Camera", "second")
    merged = _consolidated_result([first, second])

    assert ProcessingSessionController._is_session_reconstruction(merged)
    assert ProcessingSessionController._multidata_source(str(path), None) == (
        str(path),
        "Camera",
    )


def test_session_multidata_source_does_not_guess_ambiguous_dataset(tmp_path):
    from imswitch.improcess.controller.ProcessingSessionController import (
        ProcessingSessionController,
    )

    path = tmp_path / "multi.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("A", data=np.ones((2, 2), dtype=np.uint16))
        handle.create_dataset("B", data=np.ones((2, 2), dtype=np.uint16))

    assert ProcessingSessionController._multidata_source(str(path), None) is None


def test_dataset_favorite_persists_across_refresh_and_reopen(tmp_path):
    raw = tmp_path / "raw"
    _h5_image(raw / "good.h5")
    _h5_image(raw / "other.h5")
    results = tmp_path / "results"

    session = ProcessingSession(raw, results)
    session.refresh()
    good = next(dataset for dataset in session.datasets if dataset.label == "good.h5")
    session.set_favorite([good.id], True)

    session.refresh()
    assert session.dataset_by_id(good.id).favorite is True
    assert sum(dataset.favorite for dataset in session.datasets) == 1

    reopened = ProcessingSession(raw, results)
    reopened.refresh()
    assert reopened.dataset_by_id(good.id).favorite is True
    payload = json.loads(reopened.manifest_path.read_text(encoding="utf-8"))
    assert payload["schema"] == 5
    manifest_good = next(entry for entry in payload["datasets"] if entry["id"] == good.id)
    assert manifest_good["favorite"] is True


def test_raw_cleanup_plan_skips_nonfavorite_dataset_sharing_container_with_kept_data(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    shared = raw / "shared.h5"
    with h5py.File(shared, "w") as handle:
        for name in ("A", "B"):
            image = handle.create_dataset(name, data=np.ones((2, 4, 5), dtype=np.uint16))
            image.attrs["axes"] = ["Z", "Y", "X"]

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    assert len(session.datasets) == 2
    first, second = sorted(session.datasets, key=lambda dataset: dataset.primary_dataset)
    session.set_favorite([first.id], True)

    deletable, paths, skipped = session.raw_cleanup_plan([second.id])

    assert deletable == []
    assert paths == []
    assert [dataset.id for dataset in skipped] == [second.id]


def test_raw_cleanup_plan_allows_shared_container_when_all_owners_are_removed(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    shared = raw / "shared.h5"
    with h5py.File(shared, "w") as handle:
        for name in ("A", "B"):
            image = handle.create_dataset(name, data=np.ones((2, 4, 5), dtype=np.uint16))
            image.attrs["axes"] = ["Z", "Y", "X"]

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    ids = [dataset.id for dataset in session.datasets]

    deletable, paths, skipped = session.raw_cleanup_plan(ids)

    assert {dataset.id for dataset in deletable} == set(ids)
    assert paths == [str(shared)]
    assert skipped == []


def test_saved_result_preserves_reconstruction_recipe_identity(tmp_path):
    from imswitch.improcess.model.provenance import result_recipe_ids

    raw = tmp_path / "raw"
    results = tmp_path / "results"
    path = _h5_image(raw / "scan.h5")
    saved = _saved_reconstruction(
        path, results, "recipe", recon=_Recon(), params={"gain": 3}
    )

    restored = load_saved_result(saved)
    recipe_ids = result_recipe_ids(restored)

    assert len(recipe_ids) == 1
    assert recipe_ids[0].startswith("sha256:")

    session = ProcessingSession(raw, results)
    session.refresh()
    assert session.datasets[0].saved_reconstructions[0].recipe_ids == recipe_ids


def test_register_saved_result_does_not_rescan_existing_result_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from imswitch.improcess.model import processing_session as session_module
    from imswitch.improcess.model.provenance import PROVENANCE_KEY

    raw = tmp_path / "raw"
    results = tmp_path / "results"
    path = _h5_image(raw / "scan.h5")
    session = ProcessingSession(raw, results)
    session.refresh()
    dataset = session.datasets[0]

    result = SimpleNamespace(
        name="reconstruction",
        metadata={
            PROVENANCE_KEY: {
                "nodes": {
                    "recon": {
                        "op": "reconstruct",
                        "label": "Test reconstructor",
                        "params": {},
                        "recipe_id": "sha256:test",
                    }
                },
                "output": {"node": "recon"},
            }
        },
    )

    monkeypatch.setattr(
        session_module,
        "read_provenance",
        lambda _path: (_ for _ in ()).throw(AssertionError("unexpected result rescan")),
    )

    saved = session.register_saved_result(
        dataset.id,
        results / "scan_recon.ome.tif",
        result=result,
        display_name="reconstruction",
        save_manifest=False,
    )

    assert saved is not None
    assert saved.recipe_ids == ("sha256:test",)
    assert dataset.saved_reconstructions == [saved]


def test_reconcile_path_can_skip_historical_result_rescan_on_watcher_hot_path(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    results = tmp_path / "results"
    raw.mkdir()
    session = ProcessingSession(raw, results)
    session.refresh()
    path = _h5_image(raw / "new_scan.h5")

    monkeypatch.setattr(
        session,
        "reconcile_results",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("watcher hot path must not rescan historical results")
        ),
    )

    assert session.reconcile_path(path, reconcile_saved_results=False)
    assert len(session.datasets) == 1


def test_explicit_queue_claim_resolves_partitioned_lapse_when_detector_provenance_does_not(tmp_path):
    """Runtime queue ownership outranks ambiguous partition-local provenance."""
    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / "lapse_Camera.h5"
    _single_file_lapse(path, 3)

    session = ProcessingSession(raw, tmp_path / "results")
    session.refresh()
    dataset = session.datasets[0]

    # This mirrors the former live provenance: the file path was right, but
    # ``CAM`` names the detector rather than the logical scanN/CAM time lapse.
    result = _recorded_result_for_source(path, "CAM", "lapse")
    assert session.dataset_for_result(result) is None

    result._session_dataset_id = dataset.id
    result._session_origin = str(session.manifest_path.resolve())
    assert session.dataset_for_result(result).id == dataset.id

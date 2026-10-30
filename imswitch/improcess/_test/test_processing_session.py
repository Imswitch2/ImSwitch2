from __future__ import annotations

from pathlib import Path
import json

import h5py
import numpy as np
import pytest

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.processing_session import ProcessingSession
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
    assert migrated["schema"] == 3
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

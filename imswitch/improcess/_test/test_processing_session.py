from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.processing_session import ProcessingSession
from imswitch.improcess.model.provenance import PROVENANCE_KEY, record_reconstruction
from imswitch.improcess.model.saved_result import load_saved_result


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


def test_saved_result_is_associated_by_provenance_and_final_persists(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    saved = _saved_reconstruction(raw_path, results)

    session = ProcessingSession(raw_root, results)
    session.refresh()

    dataset = session.datasets[0]
    assert [Path(item.path) for item in dataset.saved_reconstructions] == [saved]
    # The common one-reconstruction-per-raw workflow resolves automatically.
    assert dataset.status == "final"
    assert dataset.final_reconstruction is not None

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()
    assert reopened.datasets[0].status == "final"
    assert reopened.datasets[0].final_reconstruction is not None


def test_restore_saved_hdf5_result_is_lazy_and_keeps_provenance(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    saved = _saved_reconstruction(raw_path, results)
    # Coordinate datasets are legal companions of a saved time-series result;
    # the image loader must still choose the canonical ``data`` dataset.
    with h5py.File(saved, "a") as handle:
        handle.create_dataset("t_seconds", data=np.arange(3, dtype=float))

    restored = load_saved_result(saved)

    assert restored.data.shape == (4, 5)
    assert not isinstance(restored.data, np.ndarray)
    assert restored.metadata.get(PROVENANCE_KEY) is not None
    assert restored.name == "full"
    assert Path(restored._session_saved_path) == saved.resolve()


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


def test_second_saved_result_enters_review_and_multiple_finals_are_allowed(tmp_path):
    raw_root = tmp_path / "raw"
    results = tmp_path / "results"
    raw_path = _h5_image(raw_root / "scan01.h5")
    first = _saved_reconstruction(raw_path, results, "full")

    session = ProcessingSession(raw_root, results)
    session.refresh()
    dataset = session.datasets[0]
    assert dataset.status == "final"
    assert [item.name for item in dataset.final_reconstructions] == ["full"]

    second = _saved_reconstruction(raw_path, results, "full_test")
    session.refresh()
    dataset = session.datasets[0]
    assert dataset.status == "review"
    assert [item.name for item in dataset.final_reconstructions] == ["full"]
    assert [item.name for item in dataset.candidate_reconstructions] == ["full_test"]

    assert session.set_final(dataset.id, second, True)
    assert dataset.status == "final"
    assert {item.name for item in dataset.final_reconstructions} == {"full", "full_test"}

    reopened = ProcessingSession(raw_root, results)
    reopened.refresh()
    assert reopened.datasets[0].status == "final"
    assert {item.name for item in reopened.datasets[0].final_reconstructions} == {
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
    assert dataset.status == "review"

    trashed = session.trash_reconstruction(dataset.id, second)
    assert trashed is not None
    assert trashed.exists()
    assert ".trash" in trashed.parts
    assert not second.exists()

    session.refresh()
    dataset = session.datasets[0]
    assert dataset.status == "final"
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

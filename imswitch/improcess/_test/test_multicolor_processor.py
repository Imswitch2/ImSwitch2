from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.analysis.multicolor import (
    apply_alignment,
    apply_alignment_to_result_data,
    beads_to_global,
    default_bounds,
    default_x_bounds,
    detect_beads,
    extract_alignment,
    load_alignment,
    parse_bounds,
    parse_x_bounds,
    save_alignment,
    split_rois,
    split_x_rois,
    transform_details,
)
from imswitch.improcess.model import ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.multicolor_apply import (
    MulticolorApplyProcessor,
    MulticolorApplyResult,
)
from imswitch.improcess.processors.multicolor_registration import (
    MulticolorRegistrationProcessor,
    MulticolorRegistrationResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def _identity_alignment(x_bounds=None):
    return {
        "x_bounds": x_bounds or [0, 2, 4, 6],
        "reference_channel": 0,
        "mode": "maxproj",
        "roi_width": 2,
        "source_shape": (2, 3, 6),
        "transforms": [
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
        ],
    }


def test_split_x_rois_uses_minimum_width():
    volume = np.arange(2 * 3 * 9, dtype=np.float32).reshape(2, 3, 9)

    rois = split_x_rois(volume, [0, 2, 6, 9])

    assert [roi.shape for roi in rois] == [(2, 3, 2), (2, 3, 2), (2, 3, 2)]
    assert np.array_equal(rois[0], volume[..., 0:2])
    assert np.array_equal(rois[1], volume[..., 2:4])
    assert np.array_equal(rois[2], volume[..., 6:8])


def test_default_and_parsed_x_bounds():
    assert default_x_bounds(12) == [0, 4, 8, 12]
    assert parse_x_bounds("", 12) == [0, 4, 8, 12]
    assert parse_x_bounds("0,3,7,0", 12) == [0, 3, 7, 12]


def test_alignment_hdf5_roundtrip(tmp_path):
    alignment = _identity_alignment()
    path = tmp_path / "alignment.h5"

    save_alignment(alignment, path)
    loaded = load_alignment(path)

    assert loaded["x_bounds"] == alignment["x_bounds"]
    assert loaded["mode"] == "maxproj"
    assert loaded["reference_channel"] == 0
    assert np.array_equal(loaded["transforms"][1]["affine_2d"], np.eye(3))


def test_apply_alignment_splits_three_color_volume():
    volume = np.zeros((2, 3, 6), dtype=np.float32)
    volume[..., 0:2] = 1
    volume[..., 2:4] = 2
    volume[..., 4:6] = 3

    aligned = apply_alignment(volume, _identity_alignment())

    assert aligned.shape == (3, 2, 3, 2)
    assert np.all(aligned[0] == 1)
    assert np.all(aligned[1] == 2)
    assert np.all(aligned[2] == 3)


def test_apply_alignment_to_time_series():
    data = np.zeros((2, 2, 3, 6), dtype=np.float32)
    data[0, ..., 0:2] = 1
    data[0, ..., 2:4] = 2
    data[0, ..., 4:6] = 3
    data[1] = data[0] + 10

    aligned = apply_alignment_to_result_data(data, ["T", "Z", "Y", "X"], _identity_alignment())

    assert aligned.shape == (2, 3, 2, 3, 2)
    assert np.all(aligned[0, 1] == 2)
    assert np.all(aligned[1, 2] == 13)


def test_multicolor_processors_are_registered():
    ids = available_processor_ids()

    assert "multicolor-registration" in ids
    assert "multicolor-apply" in ids


def test_multicolor_apply_processor_loads_alignment_file(tmp_path):
    path = tmp_path / "alignment.h5"
    save_alignment(_identity_alignment(), path)
    data = np.zeros((2, 3, 6), dtype=np.float32)
    data[..., 0:2] = 1
    data[..., 2:4] = 2
    data[..., 4:6] = 3
    result = MinimalResult(
        name="deskewed",
        data=data,
        axis_labels=["Z", "Y", "X"],
        axis_scales=[0.1, 0.1, 0.1],
        scale_unit="um",
    )

    aligned = MulticolorApplyProcessor().apply(result, {"alignment_path": str(path)})

    assert isinstance(aligned, MulticolorApplyResult)
    assert aligned.data.shape == (3, 2, 3, 2)
    assert aligned.axis_labels == ["C", "Z", "Y", "X"]
    assert aligned.axis_scales == [1.0, 0.1, 0.1, 0.1]
    assert aligned.scale_unit == "um"


def test_multicolor_registration_processor_can_use_mocked_alignment(monkeypatch):
    data = np.zeros((2, 3, 6), dtype=np.float32)
    data[..., 0:2] = 1
    data[..., 2:4] = 2
    data[..., 4:6] = 3
    result = MinimalResult(
        name="beads",
        data=data,
        axis_labels=["Z", "Y", "X"],
        axis_scales=[0.2, 0.2, 0.2],
        scale_unit="um",
    )

    def fake_extract_alignment(*args, **kwargs):
        return _identity_alignment()

    monkeypatch.setattr(
        "imswitch.improcess.processors.multicolor_registration.processor.extract_alignment",
        fake_extract_alignment,
    )

    registered = MulticolorRegistrationProcessor().apply(result, {"x_bounds": "0,2,4,6"})

    assert isinstance(registered, MulticolorRegistrationResult)
    assert registered.data.shape == (3, 2, 3, 2)
    assert registered.axis_labels == ["C", "Z", "Y", "X"]


def _identity_alignment_y2():
    return {
        "x_bounds": [0, 3, 6],
        "split_axis": "Y",
        "reference_channel": 0,
        "mode": "maxproj",
        "roi_width": 3,
        "source_shape": (2, 6, 4),
        "transforms": [
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
        ],
        "diagnostics": {
            "bead_counts": [7, 6],
            "matched_counts": [None, 6],
            "inlier_counts": [None, 5],
        },
    }


def _synthetic_bead_volume():
    volume = np.zeros((12, 40, 30), dtype=np.float32)
    local = [(2, 5, 3), (9, 30, 8), (5, 12, 1), (3, 33, 6), (10, 8, 5)]
    for strip in range(3):
        for z, y, x in local:
            volume[z, y, strip * 10 + x] = 100.0
    return volume, [0, 10, 20, 30], local


def test_split_rois_along_y_axis():
    volume = np.arange(2 * 8 * 3, dtype=np.float32).reshape(2, 8, 3)

    rois = split_rois(volume, [0, 4, 8], axis="Y")

    assert [roi.shape for roi in rois] == [(2, 4, 3), (2, 4, 3)]
    assert np.array_equal(rois[0], volume[:, 0:4, :])
    assert np.array_equal(rois[1], volume[:, 4:8, :])


def test_default_and_parsed_bounds_n_slices():
    assert default_bounds(12, 4) == [0, 3, 6, 9, 12]
    assert parse_bounds("", 10, 2) == [0, 5, 10]
    assert parse_bounds("0,3,6,0", 12, 3) == [0, 3, 6, 12]
    with pytest.raises(ValueError):
        parse_bounds("0,5,10", 10, 3)


def test_detect_beads_and_global_coords():
    volume, bounds, local = _synthetic_bead_volume()

    coords = detect_beads(
        volume, bounds, axis="X", sigma=1.0, min_distance=2, threshold_rel=0.3
    )

    assert [len(channel_coords) for channel_coords in coords] == [5, 5, 5]
    assert {tuple(point) for point in coords[1]} == set(local)
    global_coords = beads_to_global(coords, bounds, axis="X")
    assert {tuple(point) for point in global_coords[1]} == {
        (float(z), float(y), float(x + 10)) for z, y, x in local
    }


def test_two_step_descriptor_registration_uses_found_beads():
    volume, bounds, _local = _synthetic_bead_volume()
    coords = detect_beads(
        volume, bounds, axis="X", sigma=1.0, min_distance=2, threshold_rel=0.3
    )

    alignment = extract_alignment(
        volume,
        bounds,
        mode="descriptor_3d",
        reference_channel=0,
        match_max_dist=5.0,
        ransac_n_iter=200,
        ransac_inlier_px=2.0,
        bead_coords=coords,
    )

    assert alignment["diagnostics"]["bead_counts"] == [5, 5, 5]
    assert alignment["diagnostics"]["matched_counts"][1] == 5
    affine = alignment["transforms"][1]["affine_3d"]
    assert np.allclose(affine[:, :3], np.eye(3), atol=0.05)
    assert np.allclose(affine[:, 3], 0.0, atol=0.5)


def test_alignment_roundtrip_two_channel_y_axis(tmp_path):
    alignment = _identity_alignment_y2()
    path = tmp_path / "alignment_y.h5"

    save_alignment(alignment, path)
    loaded = load_alignment(path)

    assert loaded["split_axis"] == "Y"
    assert len(loaded["transforms"]) == 2
    assert loaded["x_bounds"] == [0, 3, 6]
    assert loaded["diagnostics"]["bead_counts"] == [7, 6]
    assert loaded["diagnostics"]["matched_counts"] == [None, 6]


def test_apply_alignment_splits_along_y():
    volume = np.zeros((2, 6, 4), dtype=np.float32)
    volume[:, 0:3] = 1
    volume[:, 3:6] = 2

    aligned = apply_alignment(volume, _identity_alignment_y2())

    assert aligned.shape == (2, 2, 3, 4)
    assert np.all(aligned[0] == 1)
    assert np.all(aligned[1] == 2)


def test_transform_details_reports_beads_and_transforms():
    text = transform_details(_identity_alignment_y2())

    assert "Beads found: ch0=7, ch1=6" in text
    assert "ch0: reference (identity)" in text
    assert "ch1: z_shift=0" in text
    assert "[6 matched, 5 inlier(s)]" in text


def test_multicolor_registration_processor_supports_split_params(monkeypatch):
    data = np.zeros((2, 6, 4), dtype=np.float32)
    result = MinimalResult(
        name="beads",
        data=data,
        axis_labels=["Z", "Y", "X"],
        axis_scales=[0.2, 0.2, 0.2],
        scale_unit="um",
    )
    captured = {}

    def fake_extract_alignment(volume, bounds, **kwargs):
        captured["bounds"] = bounds
        captured["split_axis"] = kwargs.get("split_axis")
        return _identity_alignment_y2()

    monkeypatch.setattr(
        "imswitch.improcess.processors.multicolor_registration.processor.extract_alignment",
        fake_extract_alignment,
    )

    registered = MulticolorRegistrationProcessor().apply(
        result, {"n_slices": 2, "split_axis": "Y"}
    )

    assert captured["bounds"] == [0, 3, 6]
    assert captured["split_axis"] == "Y"
    assert registered.data.shape == (2, 2, 3, 4)


def test_multicolor_registration_result_saves_alignment_preview(tmp_path):
    data = np.zeros((3, 2, 3, 2), dtype=np.float32)
    result = MulticolorRegistrationResult(
        "registration",
        data=data,
        alignment=_identity_alignment(),
        params={"mode": "maxproj"},
    )
    path = tmp_path / "registration.h5"

    result.save(path, "hdf5")

    with h5py.File(path, "r") as h5:
        assert "x_bounds" in h5
        assert "aligned_preview" in h5
        assert h5.attrs["mode"] == "maxproj"

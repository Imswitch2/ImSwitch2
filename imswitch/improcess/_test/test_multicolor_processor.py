from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.multicolor import (
    apply_alignment,
    apply_alignment_to_result_data,
    default_x_bounds,
    load_alignment,
    parse_x_bounds,
    save_alignment,
    split_x_rois,
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

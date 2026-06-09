from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc, split_single_image
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.frc import FRCProcessor, FRCResult


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def _spot_image(shape=(64, 64), sigma=4.0):
    y, x = np.indices(shape)
    image = np.zeros(shape, dtype=np.float64)
    for cy, cx, amp in [(20, 22, 1.0), (42, 35, 0.8), (28, 48, 0.6)]:
        image += amp * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * sigma**2))
    return image


def test_two_image_frc_identical_images_stays_high():
    image = _spot_image()

    analysis = frc_two_image(image, image, window="hann")

    finite = np.isfinite(analysis.frc)
    assert finite.any()
    assert np.nanmedian(analysis.frc[finite]) > 0.99
    assert np.isnan(analysis.resolution)


def test_single_image_split_returns_same_shape_halves():
    image = _spot_image()

    a, b = split_single_image(image, split="checkerboard")

    assert a.shape == image.shape
    assert b.shape == image.shape
    assert np.count_nonzero(a) > 0
    assert np.count_nonzero(b) > 0
    assert np.all((a == 0) | (b == 0))


def test_single_image_frc_returns_curve_and_metadata():
    image = _spot_image()

    analysis = single_image_frc(image, split="odd-even", window="hann")

    assert analysis.frequency.shape == analysis.frc.shape
    assert analysis.threshold.shape == analysis.frc.shape
    assert analysis.metadata["mode"] == "single-image"
    assert analysis.metadata["split"] == "odd-even"


def test_frc_processor_registered_and_generates_graph_payload():
    data = np.stack([_spot_image(), _spot_image() + 0.01], axis=0).astype(np.float32)
    result = MinimalResult(name="stack", data=data, axis_labels=["T", "Y", "X"])
    processor = FRCProcessor()

    frc_result = processor.apply(
        result,
        {
            "mode": "two-image",
            "compare_axis": "T",
            "index_a": 0,
            "index_b": 1,
            "window": "hann",
            "pixel_size": 50.0,
            "resolution_unit": "nm",
        },
    )

    assert "frc" in available_processor_ids()
    assert isinstance(frc_result, FRCResult)
    assert frc_result.data.shape[0] == 3
    assert frc_result.analysis.resolution_unit == "nm"
    payloads = frc_result.plot_payloads()
    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)
    assert [series.name for series in payloads[0].series[:2]] == ["FRC", "1/7 threshold"]


def test_frc_result_saves_hdf5(tmp_path):
    analysis = frc_two_image(_spot_image(), _spot_image(), window="hann")
    result = FRCResult("frc", analysis, params={"mode": "test"})
    out_path = tmp_path / "frc.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert {"frequency", "frc", "threshold"}.issubset(h5.keys())
        assert h5.attrs["mode"] == "test"
        assert h5.attrs["resolution_unit"] == "px"

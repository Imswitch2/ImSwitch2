from pathlib import Path

import h5py
import numpy as np
import pytest

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


def test_two_image_frc_compares_two_separate_reconstructions():
    """The common case: two reconstructions of the same field, each a result
    of its own. Before the input-arity work this could only be expressed as
    two planes of one stack, so it could not be done at all."""
    first = MinimalResult(
        name="recA", data=_spot_image().astype(np.float32), axis_labels=["Y", "X"]
    )
    second = MinimalResult(
        name="recB",
        data=(_spot_image() + 0.01).astype(np.float32),
        axis_labels=["Y", "X"],
    )

    frc_result = FRCProcessor().apply(
        first,
        {
            "mode": "two-image",
            "results": [first, second],
            "window": "hann",
            "pixel_size": 50.0,
            "resolution_unit": "nm",
        },
    )

    assert isinstance(frc_result, FRCResult)
    assert frc_result.name == "recA (FRC vs recB)"
    assert frc_result.analysis.resolution_unit == "nm"


def test_two_image_frc_across_results_indexes_a_stacked_input():
    """A stack paired with a plain image: the stack is indexed along its
    compare axis, the single plane is taken as it is."""
    stack = MinimalResult(
        name="stack",
        data=np.stack([_spot_image(), np.zeros((64, 64))]).astype(np.float32),
        axis_labels=["T", "Y", "X"],
    )
    plain = MinimalResult(
        name="plain", data=_spot_image().astype(np.float32), axis_labels=["Y", "X"]
    )

    frc_result = FRCProcessor().apply(
        stack,
        {
            "mode": "two-image",
            "results": [stack, plain],
            "compare_axis": "T",
            "index_a": 0,
            "window": "hann",
        },
    )

    # Plane 0 of the stack against the identical plain image: perfectly
    # correlated, so the curve never falls through the threshold.
    assert np.isnan(frc_result.analysis.resolution)


def test_two_image_frc_across_results_rejects_mismatched_sizes():
    first = MinimalResult(
        name="recA", data=_spot_image().astype(np.float32), axis_labels=["Y", "X"]
    )
    small = MinimalResult(
        name="small",
        data=_spot_image((32, 32)).astype(np.float32),
        axis_labels=["Y", "X"],
    )

    ok, reason = FRCProcessor().check_inputs([first, small])

    assert ok is False
    assert "64x64" in reason and "32x32" in reason


def test_frc_result_saves_hdf5(tmp_path):
    analysis = frc_two_image(_spot_image(), _spot_image(), window="hann")
    result = FRCResult("frc", analysis, params={"mode": "test"})
    out_path = tmp_path / "frc.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert {"frequency", "frc", "threshold"}.issubset(h5.keys())
        assert h5.attrs["mode"] == "test"
        assert h5.attrs["resolution_unit"] == "px"


def _noisy_spots(seed=0):
    rng = np.random.default_rng(seed)
    return (_spot_image() + 0.05 * rng.random((64, 64))).astype(np.float32)


def _calibrated(name="cal", scale=65.0, unit="nm", data=None, scales=None):
    data = _noisy_spots() if data is None else data
    return MinimalResult(
        name=name,
        data=data,
        axis_labels=["Y", "X"],
        axis_scales=list(scales or (scale, scale)),
        scale_unit=unit,
    )


def _frc(result, **params):
    return FRCProcessor().apply(result, {**FRCProcessor.default_params(), **params}).analysis


def test_frc_defaults_take_the_pixel_size_and_unit_from_the_image():
    in_px = _frc(MinimalResult(name="raw", data=_noisy_spots(), axis_labels=["Y", "X"]))
    in_nm = _frc(_calibrated(scale=65.0, unit="nm"))

    assert (in_px.pixel_size, in_px.resolution_unit) == (1.0, "px")
    assert (in_nm.pixel_size, in_nm.resolution_unit) == (65.0, "nm")
    assert in_nm.frequency_unit == "cycles/nm"
    assert np.isfinite(in_px.resolution)
    assert in_nm.resolution == pytest.approx(in_px.resolution * 65.0)


def test_frc_converts_the_image_calibration_to_the_chosen_unit():
    image_unit = _frc(_calibrated(scale=0.065, unit="µm"))
    in_nm = _frc(_calibrated(scale=0.065, unit="µm"), resolution_unit="nm")

    assert (image_unit.pixel_size, image_unit.resolution_unit) == (0.065, "um")
    assert in_nm.pixel_size == pytest.approx(65.0)
    assert in_nm.resolution == pytest.approx(image_unit.resolution * 1000.0)


def test_frc_explicit_pixel_size_and_unit_win_over_the_calibration():
    explicit = _frc(_calibrated(scale=0.065, unit="um"), pixel_size=50.0, resolution_unit="nm")
    in_px = _frc(_calibrated(scale=65.0, unit="nm"), resolution_unit="px")
    in_image_unit = _frc(_calibrated(scale=65.0, unit="nm"), pixel_size=80.0)

    assert (explicit.pixel_size, explicit.resolution_unit) == (50.0, "nm")
    assert (in_px.pixel_size, in_px.resolution_unit) == (1.0, "px")
    assert (in_image_unit.pixel_size, in_image_unit.resolution_unit) == (80.0, "nm")


def test_frc_in_a_physical_unit_needs_a_pixel_size_for_an_uncalibrated_image():
    raw = MinimalResult(name="raw", data=_noisy_spots(), axis_labels=["Y", "X"])

    with pytest.raises(ValueError, match="'raw' has no physical pixel size.*set Pixel size"):
        _frc(raw, resolution_unit="nm")
    assert _frc(raw, pixel_size=65.0, resolution_unit="nm").pixel_size == 65.0


def test_frc_refuses_non_square_pixels_unless_a_pixel_size_is_given():
    rect = _calibrated(name="rect", scales=(100.0, 65.0), unit="nm")

    with pytest.raises(ValueError, match="square pixels: 'rect' has 100 x 65 nm"):
        _frc(rect)
    assert _frc(rect, pixel_size=65.0).pixel_size == 65.0


def test_two_image_frc_refuses_inputs_calibrated_differently():
    first = _calibrated(name="recA", scale=65.0)
    second = _calibrated(name="recB", scale=130.0, data=_noisy_spots(1))
    params = {"mode": "two-image", "results": [first, second]}

    with pytest.raises(ValueError, match="'recA' has 65 nm pixels but 'recB' has 130 nm"):
        _frc(first, **params)
    same = _frc(first, **{**params, "results": [first, _calibrated(name="recC", data=_noisy_spots(1))]})
    assert (same.pixel_size, same.resolution_unit) == (65.0, "nm")


def test_frc_params_recorded_before_calibration_keep_meaning_pixels():
    processor = FRCProcessor()

    assert FRCProcessor.params_version == 2
    migrated = processor.migrate_params({"mode": "single-image"}, 1)
    assert (migrated["pixel_size"], migrated["resolution_unit"]) == (1.0, "px")
    explicit = {"pixel_size": 50.0, "resolution_unit": "nm"}
    assert processor.migrate_params(explicit, 1) == explicit
    assert processor.migrate_params({"mode": "single-image"}, 2) == {"mode": "single-image"}

from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.analysis.psf_resolution import fit_psf, fit_psf_batch
from imswitch.improcess.analysis.roi_manager import ROIRecord
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.psf_resolution import (
    PSFMeasurementResult,
    PSFResolutionProcessor,
    PSFResolutionResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def _gaussian(shape=(31, 33), center=(14.2, 16.7), sigma=(2.4, 3.1), amplitude=80.0, background=5.0):
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    return background + amplitude * np.exp(
        -(
            ((yy - center[0]) ** 2) / (2.0 * sigma[0] ** 2)
            + ((xx - center[1]) ** 2) / (2.0 * sigma[1] ** 2)
        )
    )


def test_fit_psf_accepts_camera_dtypes():
    """Camera frames are uint16 or float32, never float64: the conversion to
    float64 must be allowed to copy (NumPy 2 raises on a no-copy request it
    cannot honour, which made the processor fail on every real image)."""
    for dtype in (np.uint16, np.float32):
        image = _gaussian(amplitude=8000.0, background=500.0).astype(dtype)
        fit = fit_psf(image, (5, 25, 6, 28), name="bead")
        assert fit.center_x == pytest.approx(16.7, abs=0.05)
        batch = fit_psf_batch(image, [ROIRecord("roi-bead", "rectangle", (5, 25, 6, 28))])
        assert len(batch.fits) == 1 and batch.fits[0].center_y == pytest.approx(14.2, abs=0.05)


def test_fit_psf_recovers_synthetic_gaussian():
    image = _gaussian()

    fit = fit_psf(image, (5, 25, 6, 28), name="bead")

    assert fit.name == "bead"
    assert fit.center_y == pytest.approx(14.2, abs=0.05)
    assert fit.center_x == pytest.approx(16.7, abs=0.05)
    assert fit.sigma_y == pytest.approx(2.4, rel=0.03)
    assert fit.sigma_x == pytest.approx(3.1, rel=0.03)
    assert fit.fwhm_y == pytest.approx(2.354820045 * 2.4, rel=0.03)
    assert fit.fit_error < 1e-4


def test_fit_psf_batch_uses_roi_names_and_scaling():
    image = _gaussian(shape=(24, 24), center=(10.0, 12.0), sigma=(1.5, 2.0))
    rois = [ROIRecord("roi-bead", "rectangle", (4, 17, 5, 20))]

    analysis = fit_psf_batch(image, rois, pixel_size=100.0, unit="nm")

    assert len(analysis.fits) == 1
    row = analysis.rows()[0]
    assert row["name"] == "roi-bead"
    assert row["fwhm_x_nm"] == pytest.approx(2.354820045 * 2.0 * 100.0, rel=0.03)
    assert analysis.metadata["source"] == "roi"


def _outputs(output):
    """Port name -> result of a ProcessorOutput."""
    return dict(zip(output.keys, output.results))


def _used(measurement):
    """The bead rows of a measurement that entered its statistics."""
    return [row for row in measurement.bead_rows() if row["selected"]]


def test_psf_resolution_processor_registered_and_fits_whole_image():
    """``full_image`` is the v1 behaviour without ROIs: one PSF for the frame."""
    image = _gaussian()
    result = MinimalResult(name="image", data=image, axis_labels=["Y", "X"])

    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "full_image", "average_psf": False}))

    assert "psf-resolution" in available_processor_ids()
    assert set(out) == {"psf"}
    psf = out["psf"]
    assert isinstance(psf, PSFMeasurementResult) and psf.kind == "table"
    (row,) = _used(psf)
    assert row["fwhm_y_px"] == pytest.approx(2.354820045 * 2.4, rel=0.03)
    assert row["fwhm_x_px"] == pytest.approx(2.354820045 * 3.1, rel=0.03)
    payloads = psf.plot_payloads()
    assert payloads and all(isinstance(p, PlotPayload) for p in payloads)


def test_psf_resolution_processor_accepts_rois_param():
    """The panel passes ROI Manager selections through params['rois']."""
    image = _gaussian(shape=(24, 24), center=(10.0, 12.0), sigma=(1.5, 2.0))
    result = MinimalResult(name="image", data=image, axis_labels=["Y", "X"])
    rois = [ROIRecord("roi-bead", "rectangle", (4, 17, 5, 20))]

    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "rois", "rois": rois}))

    assert out["psf"].analysis.source == "rois"
    (row,) = _used(out["psf"])
    assert row["fwhm_x_px"] == pytest.approx(2.354820045 * 2.0, rel=0.05)


def test_rois_source_without_rois_is_an_error():
    result = MinimalResult(name="image", data=_gaussian(), axis_labels=["Y", "X"])
    with pytest.raises(ValueError, match="no ROIs"):
        PSFResolutionProcessor().apply(result, {"source": "rois"})


def test_pixel_size_comes_from_the_data_scale():
    result = MinimalResult(
        name="image", data=_gaussian(), axis_labels=["Y", "X"], axis_scales=[0.1, 0.1], scale_unit="um"
    )
    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "full_image"}))
    (row,) = _used(out["psf"])
    assert row["fwhm_x_nm"] == pytest.approx(2.354820045 * 3.1 * 100.0, rel=0.03)


def test_pixel_size_override_wins_over_the_data_scale():
    result = MinimalResult(name="image", data=_gaussian(), axis_labels=["Y", "X"])
    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "full_image", "pixel_size_nm": 50.0}))
    (row,) = _used(out["psf"])
    assert row["fwhm_x_nm"] == pytest.approx(2.354820045 * 3.1 * 50.0, rel=0.03)


def test_v1_params_migrate():
    processor = PSFResolutionProcessor()
    whole = processor.migrate_params({"pixel_size": 0.1, "unit": "um"}, 1)
    assert whole["source"] == "full_image" and whole["pixel_size_nm"] == pytest.approx(100.0)
    assert "unit" not in whole and "pixel_size" not in whole
    assert processor.migrate_params({"pixel_size": 1.0, "unit": "px"}, 1)["pixel_size_nm"] == 0.0
    rois = [ROIRecord("roi-bead", "rectangle", (4, 17, 5, 20))]
    assert processor.migrate_params({"pixel_size": 1.0, "unit": "px", "rois": rois}, 1)["source"] == "rois"
    assert set(processor.migrate_params({}, 1)) <= processor.param_keys()


def test_output_ports_follow_the_params():
    assert PSFResolutionProcessor().output_spec({"average_psf": False}).ports == ("psf",)
    spec = PSFResolutionProcessor().output_spec({"fit_aberrations": True})
    assert spec.ports == ("psf", "average_psf")


def test_psf_resolution_result_saves_hdf5(tmp_path):
    analysis = fit_psf_batch(_gaussian(), pixel_size=1.0, unit="px")
    result = PSFResolutionResult("psf", analysis, params={"pixel_size": 1.0})
    out_path = tmp_path / "psf.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert h5.attrs["fit_count"] == 1
        assert "fits" in h5
        assert h5["fits"]["fwhm_x_px"][0] > 0
        assert h5["fits"]["fit_error"][0] < 1e-4


# --------------------------------------------------------------------------- #
# Selected-only bead table, input description, preview run
# --------------------------------------------------------------------------- #
def _bead_field(n=12, shape=(160, 160), sigma=1.8, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    image = np.full(shape, 100.0)
    centers = []
    while len(centers) < n:
        c = rng.uniform(12, shape[0] - 12, 2)
        if all(np.hypot(*(c - d)) > 30 for d in centers):
            centers.append(c)
    for cy, cx in centers:
        image += 900.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma**2))
    image = rng.poisson(image).astype(np.float32)
    image[:, :10] = 0.0  # padding, like a registered or deskewed image
    return image, centers


def test_a_measurement_is_one_result_with_a_headline_row():
    image, _ = _bead_field()
    image[80, 80] = 0  # a stray zero is still data
    result = MinimalResult(name="beads", data=image, axis_labels=["Y", "X"], axis_scales=[0.065, 0.065],
                           scale_unit="um")
    output = PSFResolutionProcessor().apply(result, {})
    assert output.keys == ("average_psf", "psf")  # the measurement last: the list ends on it
    out = _outputs(output)
    psf, averaged = out["psf"], out["average_psf"]
    assert psf.name == "beads — PSF" and averaged.name == "beads — averaged PSF"
    assert averaged.kind == "image" and averaged.metadata["beads_averaged"] == psf.averaged.n

    (headline,) = psf.table_records()
    assert headline["source"] == "beads" and headline["beads_used"] == int(psf.mask.sum())
    assert headline["beads_found"] == len(psf.analysis.beads) > headline["beads_used"] > 0
    assert {"fwhm_x_nm", "fwhm_y_nm", "fwhm_x_mad_nm", "fwhm_lat_gauss_nm"} <= set(headline)
    assert psf.data.shape == (1, len(headline))

    rows = psf.bead_rows()
    assert len(rows) == len(psf.analysis.beads)  # every candidate, with its state
    assert sum(row["selected"] for row in rows) == headline["beads_used"]
    assert {row["state"] for row in rows if not row["selected"]} - {"selected"}
    assert {"fwhm_x_hm_nm", "fwhm_lat_nm"} <= set(rows[0])
    metrics = {row["metric"] for row in psf.summary_rows()}
    assert "FWHM lateral (half maximum)" in metrics and "beads selected" in metrics
    assert any(metric.startswith("rejected: ") for metric in metrics)
    assert "candidates selected" in psf.report()


def test_a_measurement_shows_the_image_with_a_marker_per_candidate():
    image, _ = _bead_field()
    result = MinimalResult(name="beads", data=image, axis_labels=["Y", "X"], axis_scales=[0.065, 0.065],
                           scale_unit="um")
    psf = _outputs(PSFResolutionProcessor().apply(result, {}))["psf"]
    context, markers = psf.display_layers()
    assert context.role == "context" and np.shape(context.data) == image.shape
    assert context.axis_scales == pytest.approx([0.065, 0.065]) and context.scale_unit == "um"
    assert markers.kind == "points" and np.shape(markers.data) == (len(psf.analysis.beads), 2)
    states = markers.layer_kwargs["features"]["state"]
    assert states.count("selected") == int(psf.mask.sum())
    assert any(title.startswith("Bead FWHM, lateral") for title in (p.title for p in psf.plot_payloads()))


def test_reselecting_a_measurement_makes_a_new_one():
    from imswitch.improcess.processors import PSFBeadSelectProcessor

    image, _ = _bead_field()
    result = MinimalResult(name="beads", data=image, axis_labels=["Y", "X"])
    psf = _outputs(PSFResolutionProcessor().apply(result, {}))["psf"]
    processor = PSFBeadSelectProcessor()
    assert processor.accepts(psf) and not processor.accepts(result)
    assert not PSFResolutionProcessor().accepts(psf)  # a measurement is not an image

    output = processor.apply(psf, {"min_r2": 0.999})
    assert output.keys[-1] == "psf"
    again = _outputs(output)["psf"]
    assert again.name == "beads — PSF (re-selected)" and again.source_name == "beads"
    assert again.analysis is psf.analysis  # no refit
    assert again.summary["n_selected"] < psf.summary["n_selected"]
    assert again.params["min_r2"] == 0.999


def test_input_layout_says_what_is_analysed():
    from imswitch.improcess.processors.psf_resolution import input_layout

    stack = MinimalResult(name="s", data=np.zeros((4, 5, 6)), axis_labels=["Z", "Y", "X"],
                          axis_scales=[0.2, 0.1, 0.1], scale_unit="um")
    layout = input_layout(stack, {})
    assert layout.is3d and layout.pixel_size == (200.0, 100.0, 100.0) and layout.from_metadata
    assert "from metadata" in layout.describe()
    assert input_layout(stack, {"pixel_size_nm": 50.0}).pixel_size == (200.0, 50.0, 50.0)
    channels = MinimalResult(name="c", data=np.zeros((4, 5, 6)), axis_labels=["C", "Y", "X"])
    layout = input_layout(channels, {})
    assert not layout.is3d and layout.pixel_size is None
    assert "label it Z" in layout.note


def test_preview_run_matches_the_fit():
    from imswitch.improcess.processors.psf_resolution import run_bead_analysis

    image, _ = _bead_field()
    result = MinimalResult(name="beads", data=image, axis_labels=["Y", "X"])
    run = run_bead_analysis(result, {})
    out = _outputs(PSFResolutionProcessor().apply(result, {}))
    assert list(run.mask) == list(out["psf"].mask)
    assert len(run.reasons) == len(run.analysis.beads)
    assert all(r == "" for r, m in zip(run.reasons, run.mask) if m)
    assert any(r == "border" for r in run.reasons)  # the beads cut by the padding


def test_a_stack_without_z_is_not_cut_to_its_first_plane():
    from imswitch.improcess.processors.psf_resolution import input_layout, run_bead_analysis

    rng = np.random.default_rng(4)
    yy, xx = np.mgrid[:120, :120]
    stack = np.full((9, 120, 120), 100.0)
    for (cy, cx), plane in zip([(30, 30), (30, 90), (90, 30), (90, 90)], (1, 4, 6, 8)):
        stack[plane] += 900.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 1.8**2))
    result = MinimalResult(name="frames", data=rng.poisson(stack).astype(np.float32),
                           axis_labels=["Frame", "Y", "X"])
    layout = input_layout(result, {})
    assert layout.stack_axis == 0 and layout.shape == (9, 120, 120)
    assert "maximum projection" in layout.describe() and "label it Z" in layout.note
    run = run_bead_analysis(result, {})
    assert sorted(b["plane"] for b, m in zip(run.analysis.beads, run.mask) if m) == [1, 4, 6, 8]
    psf = _outputs(PSFResolutionProcessor().apply(result, {}))["psf"]
    assert all("plane" in row for row in psf.bead_rows()) and len(_used(psf)) == 4
    context, _markers = psf.display_layers()
    assert np.shape(context.data) == (120, 120)  # the projection the beads were found on


def _calibrated_stack():
    return MinimalResult(name="s", data=np.zeros((5, 20, 20)), axis_labels=["Z", "Y", "X"],
                         axis_scales=[0.2, 0.1, 0.1], scale_unit="um")


def test_aberration_requirements_say_what_is_missing():
    from imswitch.improcess.processors.psf_resolution import aberration_requirements, input_layout

    stack = _calibrated_stack()
    layout = input_layout(stack, {})
    assert "NA and the emission wavelength" in aberration_requirements(layout, {})
    assert "emission wavelength" in aberration_requirements(layout, {"na": 1.0})
    assert aberration_requirements(layout, {"na": 1.0, "wavelength_nm": 515.0}) == ""
    plane = MinimalResult(name="p", data=np.zeros((20, 20)), axis_labels=["Y", "X"])
    assert "z-stack" in aberration_requirements(input_layout(plane, {}), {"na": 1.0, "wavelength_nm": 515.0})
    uncalibrated = MinimalResult(name="u", data=np.zeros((5, 20, 20)), axis_labels=["Z", "Y", "X"])
    assert "pixel size" in aberration_requirements(input_layout(uncalibrated, {}), {"na": 1, "wavelength_nm": 5})


def test_a_skipped_aberration_fit_is_reported_in_place_of_the_results():
    image, _ = _bead_field()
    result = MinimalResult(name="beads", data=image, axis_labels=["Y", "X"])
    out = _outputs(PSFResolutionProcessor().apply(result, {"fit_aberrations": True}))
    assert out["psf"].aberrations is None
    report = out["psf"].report()
    assert "Aberrations: not estimated" in report and "z-stack" in report

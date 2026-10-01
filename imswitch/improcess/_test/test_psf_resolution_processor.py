from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.analysis.psf_resolution import fit_psf, fit_psf_batch
from imswitch.improcess.analysis.roi_manager import ROIRecord
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.psf_resolution import (
    BeadTableResult,
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


def test_psf_resolution_processor_registered_and_fits_whole_image():
    """``full_image`` is the v1 behaviour without ROIs: one PSF for the frame."""
    image = _gaussian()
    result = MinimalResult(name="image", data=image, axis_labels=["Y", "X"])

    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "full_image", "average_psf": False}))

    assert "psf-resolution" in available_processor_ids()
    assert set(out) == {"beads", "summary"}
    beads = out["beads"]
    assert isinstance(beads, BeadTableResult) and beads.kind == "table"
    (row,) = beads.table_records()
    assert row["fwhm_y_px"] == pytest.approx(2.354820045 * 2.4, rel=0.03)
    assert row["fwhm_x_px"] == pytest.approx(2.354820045 * 3.1, rel=0.03)
    payloads = beads.plot_payloads()
    assert payloads and all(isinstance(p, PlotPayload) for p in payloads)


def test_psf_resolution_processor_accepts_rois_param():
    """The panel passes ROI Manager selections through params['rois']."""
    image = _gaussian(shape=(24, 24), center=(10.0, 12.0), sigma=(1.5, 2.0))
    result = MinimalResult(name="image", data=image, axis_labels=["Y", "X"])
    rois = [ROIRecord("roi-bead", "rectangle", (4, 17, 5, 20))]

    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "rois", "rois": rois}))

    assert out["beads"].analysis.source == "rois"
    (row,) = out["beads"].table_records()
    assert row["status"] == "ok"
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
    (row,) = out["beads"].table_records()
    assert row["fwhm_x_nm"] == pytest.approx(2.354820045 * 3.1 * 100.0, rel=0.03)


def test_pixel_size_override_wins_over_the_data_scale():
    result = MinimalResult(name="image", data=_gaussian(), axis_labels=["Y", "X"])
    out = _outputs(PSFResolutionProcessor().apply(result, {"source": "full_image", "pixel_size_nm": 50.0}))
    (row,) = out["beads"].table_records()
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
    spec = PSFResolutionProcessor().output_spec({"average_psf": False})
    assert spec.ports == ("beads", "summary")
    spec = PSFResolutionProcessor().output_spec({"fit_aberrations": True})
    assert spec.ports == ("beads", "summary", "average_psf", "aberrations")


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

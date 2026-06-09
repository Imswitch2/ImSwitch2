from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.analysis.psf_resolution import fit_psf, fit_psf_batch
from imswitch.improcess.analysis.roi_manager import ROIRecord
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.psf_resolution import (
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


def test_psf_resolution_processor_registered_and_generates_payload():
    image = _gaussian()
    result = MinimalResult(name="image", data=image, axis_labels=["Y", "X"])
    processor = PSFResolutionProcessor()

    psf_result = processor.apply(result, {"pixel_size": 1.0, "unit": "px"})

    assert "psf-resolution" in available_processor_ids()
    assert isinstance(psf_result, PSFResolutionResult)
    assert psf_result.data.shape == (1, len(PSFResolutionResult._METRICS))
    payloads = psf_result.plot_payloads()
    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)
    assert payloads[0].metadata["fit_count"] == 1


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

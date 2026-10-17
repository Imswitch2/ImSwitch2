"""The preview bin a person chose survives processing, and always fits in memory."""

import numpy as np
import pytest

from imswitch.improcess.model import LocalizationResult, localizations_from_columns
from imswitch.improcess.processors.smlm_drift import SmlmDriftProcessor
from imswitch.improcess.processors.smlm_filter import SmlmFilterProcessor
from imswitch.improcess.processors.smlm_group import SmlmGroupProcessor


def _locs(n=400, extent=5000.0, seed=0, frames=40):
    rng = np.random.default_rng(seed)
    return localizations_from_columns(
        {
            "frame": rng.integers(0, frames, n),
            "x_nm": rng.uniform(0, extent, n),
            "y_nm": rng.uniform(0, extent, n),
            "photons": np.full(n, 1000.0),
        }
    )


def test_a_requested_bin_is_used_and_remembered():
    result = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=25.0)
    assert result.preview_pixel_size_nm == 25.0
    assert result.preview_pixel_size_requested_nm == 25.0
    assert 195 <= result.data.shape[0] <= 205  # 5000 nm / 25 nm


def test_an_automatic_bin_is_not_recorded_as_a_request():
    result = LocalizationResult("l", _locs(), pixel_size_nm=100.0)
    assert result.preview_pixel_size_requested_nm is None
    zero = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=0.0)
    assert zero.preview_pixel_size_requested_nm is None


@pytest.mark.parametrize(
    "run",
    [
        lambda r: SmlmFilterProcessor().apply(
            r, {"property": "photons", "mode": "bandpass", "lower": 500.0, "upper": None}
        ),
        lambda r: SmlmGroupProcessor().apply(r, SmlmGroupProcessor.default_params()),
        lambda r: SmlmDriftProcessor().apply(r, {"segments": 4, "render_pixel_size_nm": 50.0}),
    ],
    ids=["filter", "group", "drift"],
)
def test_a_derived_result_keeps_the_requested_bin(run):
    source = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=25.0)
    derived = run(source)
    assert derived.preview_pixel_size_requested_nm == 25.0
    assert derived.preview_pixel_size_nm == 25.0


def test_a_derived_result_of_an_automatic_one_stays_automatic():
    source = LocalizationResult("l", _locs(), pixel_size_nm=100.0)
    derived = SmlmFilterProcessor().apply(
        source, {"property": "photons", "mode": "bandpass", "lower": None, "upper": None}
    )
    assert derived.preview_pixel_size_requested_nm is None


def test_the_comet_result_keeps_the_requested_bin():
    pytest.importorskip("comet")
    from imswitch.improcess.processors.smlm_drift_comet import SmlmCometDriftProcessor

    source = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=25.0)
    out = SmlmCometDriftProcessor().apply(
        source, {"window_mode": "windows", "window_size": 4, "backend": "cpu"}
    )
    assert out.preview_pixel_size_requested_nm == 25.0


def test_a_bin_whose_image_would_not_fit_the_working_set_is_raised_and_recorded(monkeypatch):
    from imswitch.improcess.model import contrast

    monkeypatch.setattr(contrast, "_sample_working_set_bytes", lambda: 1024 * 1024)
    result = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=1.0)
    # 1 MiB is 262144 float32 pixels: the image fits, at a coarser bin.
    assert result.data.shape[0] * result.data.shape[1] * 4 <= 1024 * 1024
    assert result.preview_pixel_size_nm > 1.0
    adjusted = result.metadata["preview_pixel_size_adjusted"]
    assert adjusted["requested_nm"] == 1.0
    assert adjusted["used_nm"] == pytest.approx(result.preview_pixel_size_nm)
    # What was asked for is remembered, so a result made from this one asks again.
    assert result.preview_pixel_size_requested_nm == 1.0


def test_a_bin_that_fits_is_left_alone():
    result = LocalizationResult("l", _locs(), pixel_size_nm=100.0, preview_pixel_size_nm=25.0)
    assert "preview_pixel_size_adjusted" not in result.metadata

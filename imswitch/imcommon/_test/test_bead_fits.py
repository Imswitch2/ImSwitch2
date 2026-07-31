"""Numerical contracts for the shared ImControl/ImProcess BeadRec fits."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.bead_fits import (
    FIT_MODELS,
    Exponential2D,
    Sine1D,
)
from imswitch.imcommon.algorithms.bead_recognition import fit_bead


def test_new_models_are_registered_under_stable_ids():
    assert isinstance(FIT_MODELS["exponential2d"], Exponential2D)
    assert isinstance(FIT_MODELS["sine1d"], Sine1D)


def test_exponential2d_recovers_center_and_decay_length():
    height, width = 39, 43
    y, x = np.indices((height, width), dtype=float)
    image = FIT_MODELS["exponential2d"].model(
        (x, y), 120.0, 21.4, 18.2, 5.3, 7.0
    )

    result = fit_bead(
        image,
        "exponential2d",
        roi=(0, 0, width, height),
    )

    assert result.r_squared > 0.999
    assert result.params["x0"] == pytest.approx(21.4, abs=0.05)
    assert result.params["y0"] == pytest.approx(18.2, abs=0.05)
    assert result.params["decay_length"] == pytest.approx(5.3, rel=0.01)
    assert result.summary["fwhm"] == pytest.approx(2 * 5.3 * np.log(2), rel=0.01)


def test_sine1d_recovers_non_pixel_aligned_wave():
    height = width = 64
    y, x = np.indices((height, width), dtype=float)
    theta = np.deg2rad(27.0)
    image = FIT_MODELS["sine1d"].model(
        (x, y), 35.0, 12.8, theta, 0.7, 100.0
    )

    result = fit_bead(image, "sine1d")

    assert result.r_squared > 0.999
    assert result.params["wavelength"] == pytest.approx(12.8, rel=0.01)
    assert np.degrees(result.params["theta"]) == pytest.approx(27.0, abs=0.2)
    assert result.params["phase"] == pytest.approx(0.7, abs=0.05)
    assert result.summary["stripe_angle_degrees"] == pytest.approx(117.0, abs=0.2)


def test_sine1d_center_lies_on_nearest_bright_stripe():
    model = FIT_MODELS["sine1d"]
    params = {
        "amplitude": 8.0,
        "wavelength": 10.0,
        "theta": np.deg2rad(-32.0),
        "phase": 0.4,
        "offset": 3.0,
    }

    center_y, center_x = model.center_px(params, (51, 47))
    value = model.model(
        (np.asarray(center_x), np.asarray(center_y)),
        *(params[name] for name in model.param_names),
    )

    assert float(value) == pytest.approx(11.0, abs=1e-9)


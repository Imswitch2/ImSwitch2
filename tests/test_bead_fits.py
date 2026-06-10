import numpy as np
import pytest

scipy = pytest.importorskip("scipy")

from imswitch.imcontrol.model.bead_recognition import FIT_MODELS, fit_bead


def test_gaussian2d_recovers_known_parameters():
    """Test that Gaussian2D recovers known parameters on synthetic noisy data."""
    true_amplitude = 100.0
    true_x0 = 25.0
    true_y0 = 30.0
    true_sigma_x = 5.0
    true_sigma_y = 7.0
    true_theta = np.pi / 6
    true_offset = 10.0

    size = 64
    y, x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")

    cos_theta = np.cos(true_theta)
    sin_theta = np.sin(true_theta)
    u = (x - true_x0) * cos_theta + (y - true_y0) * sin_theta
    v = -(x - true_x0) * sin_theta + (y - true_y0) * cos_theta
    a = 1 / (2 * true_sigma_x**2)
    c = 1 / (2 * true_sigma_y**2)
    clean_image = true_amplitude * np.exp(-(a * u**2 + c * v**2)) + true_offset

    np.random.seed(42)
    noise = np.random.normal(0, 2, clean_image.shape)
    noisy_image = clean_image + noise

    result = fit_bead(noisy_image, "gaussian2d", roi=(0, 0, size, size))

    assert result.model == "gaussian2d"
    assert result.r_squared > 0.95

    assert abs(result.params["amplitude"] - true_amplitude) < 5.0
    assert abs(result.params["x0"] - true_x0) < 1.0
    assert abs(result.params["y0"] - true_y0) < 1.0
    
    fitted_sigmas = sorted([result.params["sigma_x"], result.params["sigma_y"]])
    true_sigmas = sorted([true_sigma_x, true_sigma_y])
    assert abs(fitted_sigmas[0] - true_sigmas[0]) < 1.5
    assert abs(fitted_sigmas[1] - true_sigmas[1]) < 1.5
    
    assert abs(result.params["offset"] - true_offset) < 3.0

    assert abs(result.center_px[0] - true_y0) < 1.0
    assert abs(result.center_px[1] - true_x0) < 1.0

    for pname in FIT_MODELS["gaussian2d"].param_names:
        assert pname in result.params
        assert pname in result.param_std
        assert result.param_std[pname] > 0

    assert "fwhm_x" in result.summary
    assert "fwhm_y" in result.summary
    assert "ellipticity" in result.summary
    assert "theta_degrees" in result.summary


def test_donut_r2_gaussian_recovers_known_parameters():
    """Test that DonutR2Gaussian recovers known parameters on synthetic data."""
    true_amplitude = 50.0
    true_x0 = 32.0
    true_y0 = 28.0
    true_sigma = 8.0
    true_offset = 5.0

    size = 64
    y, x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    r_squared = (x - true_x0) ** 2 + (y - true_y0) ** 2
    clean_image = (
        true_amplitude * r_squared * np.exp(-r_squared / (2 * true_sigma**2))
        + true_offset
    )

    np.random.seed(43)
    noise = np.random.normal(0, 1, clean_image.shape)
    noisy_image = clean_image + noise

    result = fit_bead(noisy_image, "donut_r2_gaussian", roi=(0, 0, size, size))

    assert result.model == "donut_r2_gaussian"
    assert result.r_squared > 0.95

    assert abs(result.params["amplitude"] - true_amplitude) < 5.0
    assert abs(result.params["x0"] - true_x0) < 1.0
    assert abs(result.params["y0"] - true_y0) < 1.0
    assert abs(result.params["sigma"] - true_sigma) < 1.0
    assert abs(result.params["offset"] - true_offset) < 2.0

    assert abs(result.center_px[0] - true_y0) < 1.0
    assert abs(result.center_px[1] - true_x0) < 1.0

    for pname in FIT_MODELS["donut_r2_gaussian"].param_names:
        assert pname in result.params
        assert pname in result.param_std
        assert result.param_std[pname] > 0

    assert "peak_radius" in result.summary
    assert "sigma" in result.summary


def test_fit_bead_raises_on_flat_image():
    """Test that fit_bead raises ValueError on a flat/constant image."""
    flat_image = np.ones((50, 50)) * 42.0

    with pytest.raises(ValueError, match="Cannot fit a flat/constant image"):
        fit_bead(flat_image, "gaussian2d", roi=(0, 0, 50, 50))

    with pytest.raises(ValueError, match="Cannot fit a flat/constant image"):
        fit_bead(flat_image, "donut_r2_gaussian", roi=(0, 0, 50, 50))


def test_fit_bead_roi_offset():
    """Test that ROI offset is correctly handled and center_px is in full-image coordinates."""
    true_x0 = 45.0
    true_y0 = 55.0
    true_sigma_x = 6.0
    true_sigma_y = 6.0
    true_amplitude = 80.0
    true_offset = 15.0
    true_theta = 0.0

    large_size = 100
    y, x = np.meshgrid(np.arange(large_size), np.arange(large_size), indexing="ij")
    a = 1 / (2 * true_sigma_x**2)
    c = 1 / (2 * true_sigma_y**2)
    clean_image = (
        true_amplitude * np.exp(-(a * (x - true_x0) ** 2 + c * (y - true_y0) ** 2))
        + true_offset
    )

    np.random.seed(44)
    noise = np.random.normal(0, 1.5, clean_image.shape)
    noisy_image = clean_image + noise

    roi_x0, roi_y0 = 30, 40
    roi_x1, roi_y1 = 60, 70
    roi = (roi_x0, roi_y0, roi_x1, roi_y1)

    result = fit_bead(noisy_image, "gaussian2d", roi=roi)

    assert abs(result.center_px[0] - true_y0) < 2.0
    assert abs(result.center_px[1] - true_x0) < 2.0

    assert result.r_squared > 0.90


def test_fit_models_registry():
    """Test that FIT_MODELS contains expected models."""
    assert "gaussian2d" in FIT_MODELS
    assert "donut_r2_gaussian" in FIT_MODELS

    gaussian = FIT_MODELS["gaussian2d"]
    assert gaussian.name == "gaussian2d"
    assert len(gaussian.param_names) == 7

    donut = FIT_MODELS["donut_r2_gaussian"]
    assert donut.name == "donut_r2_gaussian"
    assert len(donut.param_names) == 5


def test_fit_bead_auto_roi_off_center():
    """Auto-ROI (roi=None) must locate an off-center bead via the component
    mask and report center_px in full-image coordinates."""
    true_x0 = 70.0
    true_y0 = 35.0
    true_sigma = 4.0

    size = 100
    y, x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    clean_image = 80.0 * np.exp(
        -(((x - true_x0) ** 2) + (y - true_y0) ** 2) / (2 * true_sigma**2)
    ) + 5.0

    np.random.seed(7)
    noisy_image = clean_image + np.random.normal(0, 1, clean_image.shape)

    result = fit_bead(noisy_image, "gaussian2d")

    assert abs(result.center_px[0] - true_y0) < 1.0
    assert abs(result.center_px[1] - true_x0) < 1.0
    assert result.r_squared > 0.95


def test_fit_bead_auto_roi_falls_back_to_full_image():
    """When the component mask is rejected (blob too large), auto-ROI must
    fall back to fitting the whole image instead of raising."""
    size = 100
    y, x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    image = 50.0 * np.exp(
        -(((x - 50.0) ** 2) + (y - 50.0) ** 2) / (2 * 30.0**2)
    ) + 5.0

    result = fit_bead(image, "gaussian2d")

    assert abs(result.center_px[0] - 50.0) < 2.0
    assert abs(result.center_px[1] - 50.0) < 2.0


def test_sine2d_recovers_known_parameters():
    """Sine2D recovers wavelengths and reproduces the pattern on noisy data."""
    true_amplitude = 40.0
    true_lambda_x = 17.0
    true_phi_x = 0.7
    true_lambda_y = 23.0
    true_phi_y = -1.1
    true_offset = 100.0

    size = 128
    y, x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    clean_image = (
        true_amplitude
        * np.sin(2 * np.pi * x / true_lambda_x + true_phi_x)
        * np.sin(2 * np.pi * y / true_lambda_y + true_phi_y)
        + true_offset
    )

    np.random.seed(3)
    noisy_image = clean_image + np.random.normal(0, 2, clean_image.shape)

    result = fit_bead(noisy_image, "sine2d")

    assert result.model == "sine2d"
    assert result.r_squared > 0.95
    assert abs(result.params["lambda_x"] - true_lambda_x) < 0.5
    assert abs(result.params["lambda_y"] - true_lambda_y) < 0.5

    # Phases are only determined up to a joint (pi, pi) shift, so compare
    # the reconstructed pattern instead of raw phase values.
    fitted = (
        result.params["amplitude"]
        * np.sin(2 * np.pi * x / result.params["lambda_x"] + result.params["phi_x"])
        * np.sin(2 * np.pi * y / result.params["lambda_y"] + result.params["phi_y"])
        + result.params["offset"]
    )
    rmse = float(np.sqrt(np.mean((fitted - clean_image) ** 2)))
    assert rmse < 2.0

    # center_px must point at a pattern maximum near the image center
    cy, cx = result.center_px
    assert 0 <= cy < size and 0 <= cx < size
    val_x = np.sin(2 * np.pi * cx / result.params["lambda_x"] + result.params["phi_x"])
    val_y = np.sin(2 * np.pi * cy / result.params["lambda_y"] + result.params["phi_y"])
    assert val_x * val_y > 0.99


def test_sine2d_in_registry():
    """Sine2D is registered and exposes the expected parameters."""
    assert "sine2d" in FIT_MODELS
    sine = FIT_MODELS["sine2d"]
    assert sine.param_names == (
        "amplitude", "lambda_x", "phi_x", "lambda_y", "phi_y", "offset",
    )
    assert sine.wants_full_image


def test_fit_bead_unknown_model():
    """Test that fit_bead raises ValueError for unknown model name."""
    test_image = np.random.rand(50, 50) * 100

    with pytest.raises(ValueError, match="Unknown model"):
        fit_bead(test_image, "nonexistent_model")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

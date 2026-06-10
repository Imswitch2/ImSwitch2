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


def test_fit_bead_unknown_model():
    """Test that fit_bead raises ValueError for unknown model name."""
    test_image = np.random.rand(50, 50) * 100

    with pytest.raises(ValueError, match="Unknown model"):
        fit_bead(test_image, "nonexistent_model")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

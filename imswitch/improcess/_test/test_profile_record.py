"""Test build_profile_record pure function."""

import numpy as np
from imswitch.improcess.profile_helpers import build_profile_record


def test_line_profile_basic():
    """Known line profile with basic stats."""
    y = np.array([0, 5, 10, 5, 0])
    x = np.linspace(0, 0.8, 5)
    
    record = build_profile_record(
        "line", x, y,
        length_px=4.0,
        length_scaled=0.8,
        unit="um"
    )
    
    assert record["kind"] == "line"
    assert record["n_samples"] == 5
    assert record["length_px"] == 4.0
    assert record["length_um"] == 0.8
    assert record["min"] == 0.0
    assert record["max"] == 10.0
    assert record["mean"] == 4.0


def test_fit_metrics_passthrough():
    """Fit metrics appear as keys in record."""
    y = np.array([1, 2, 3, 2, 1])
    x = np.linspace(0, 1, 5)
    fit_metrics = {
        "fit_amplitude": 2.5,
        "fit_center": 0.5,
        "fit_sigma": 0.3,
        "fit_fwhm": 0.7
    }
    
    record = build_profile_record(
        "line", x, y,
        length_px=10.0,
        length_scaled=1.0,
        unit="px",
        fit_metrics=fit_metrics
    )
    
    assert "fit_amplitude" in record
    assert record["fit_amplitude"] == 2.5
    assert "fit_center" in record
    assert record["fit_center"] == 0.5
    assert "fit_sigma" in record
    assert record["fit_sigma"] == 0.3
    assert "fit_fwhm" in record
    assert record["fit_fwhm"] == 0.7


def test_empty_profile():
    """Empty y array returns n_samples=0 with nan stats."""
    y = np.array([])
    x = np.array([])
    
    record = build_profile_record(
        "line", x, y,
        length_px=0.0,
        length_scaled=0.0,
        unit="um"
    )
    
    assert record["n_samples"] == 0
    assert np.isnan(record["min"])
    assert np.isnan(record["max"])
    assert np.isnan(record["mean"])


def test_rectangle_profiles():
    """Rectangle produces two separate records with different kinds."""
    x_profile = np.array([1, 2, 3, 4, 5])
    y_profile = np.array([10, 20, 30])
    x_coords = np.linspace(0, 2.0, 5)
    y_coords = np.linspace(0, 1.2, 3)
    
    record_x = build_profile_record(
        "rectangle-x", x_coords, x_profile,
        length_px=5.0,
        length_scaled=2.0,
        unit="um"
    )
    
    record_y = build_profile_record(
        "rectangle-y", y_coords, y_profile,
        length_px=3.0,
        length_scaled=1.2,
        unit="um"
    )
    
    assert record_x["kind"] == "rectangle-x"
    assert record_x["n_samples"] == 5
    assert record_x["length_px"] == 5.0
    assert record_x["length_um"] == 2.0
    assert record_x["mean"] == 3.0
    
    assert record_y["kind"] == "rectangle-y"
    assert record_y["n_samples"] == 3
    assert record_y["length_px"] == 3.0
    assert record_y["length_um"] == 1.2
    assert record_y["mean"] == 20.0


def test_numpy_scalar_to_float():
    """Numpy scalars are converted to Python floats."""
    y = np.array([1.5, 2.5, 3.5])
    x = np.linspace(0, 1, 3)
    
    record = build_profile_record(
        "line", x, y,
        length_px=10.0,
        length_scaled=1.0,
        unit="px"
    )
    
    assert isinstance(record["min"], float)
    assert isinstance(record["max"], float)
    assert isinstance(record["mean"], float)
    assert not isinstance(record["min"], np.floating)

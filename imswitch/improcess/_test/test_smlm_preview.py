"""Tests for SMLM detection preview."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.smlm.preview import compute_detection_preview


def test_compute_detection_preview_returns_empty_for_none_input():
    x, y = compute_detection_preview(None, threshold=100.0, roi=7, sigma=1.0)
    assert len(x) == 0
    assert len(y) == 0


def test_compute_detection_preview_returns_empty_for_non_2d_input():
    image_3d = np.zeros((10, 10, 10))
    x, y = compute_detection_preview(image_3d, threshold=100.0, roi=7, sigma=1.0)
    assert len(x) == 0
    assert len(y) == 0


def test_compute_detection_preview_returns_empty_for_flat_image():
    image = np.zeros((50, 50))
    x, y = compute_detection_preview(image, threshold=100.0, roi=7, sigma=1.0)
    assert len(x) == 0
    assert len(y) == 0


def test_compute_detection_preview_detects_bright_gaussian_spot():
    """Verify that a bright Gaussian spot at a known (row, col) position
    produces scatter coordinates (x=col, y=row) matching the display convention.
    """
    image = np.zeros((50, 50), dtype=np.float32)
    
    # Create a bright Gaussian spot at (row=25, col=30)
    spot_row, spot_col = 25, 30
    yy, xx = np.ogrid[:50, :50]
    sigma = 2.0
    gaussian = np.exp(-((xx - spot_col)**2 + (yy - spot_row)**2) / (2 * sigma**2))
    image += gaussian * 1000.0
    
    # Detect with low threshold
    x, y = compute_detection_preview(image, threshold=50.0, roi=7, sigma=1.0)
    
    # Should detect exactly one spot
    assert len(x) == 1
    assert len(y) == 1
    
    # The scatter coordinates should match: x=col, y=row
    # Allow small tolerance due to sub-pixel localization
    assert abs(x[0] - spot_col) < 1.0, f"Expected x ≈ {spot_col}, got {x[0]}"
    assert abs(y[0] - spot_row) < 1.0, f"Expected y ≈ {spot_row}, got {y[0]}"


def test_compute_detection_preview_coordinate_orientation():
    """Document and verify the coordinate convention: detect_spots returns (row, col),
    and we plot as x=col, y=row so a spot at image position (r, c) lands on the
    correct pixel in the pyqtgraph view.
    """
    image = np.zeros((40, 60), dtype=np.float32)
    
    # Add two spots at known positions
    # Spot 1: row=10, col=20
    # Spot 2: row=30, col=40
    for r, c in [(10, 20), (30, 40)]:
        yy, xx = np.ogrid[:40, :60]
        gaussian = np.exp(-((xx - c)**2 + (yy - r)**2) / (2 * 1.5**2))
        image += gaussian * 1000.0
    
    x, y = compute_detection_preview(image, threshold=50.0, roi=7, sigma=1.0)
    
    # Should detect two spots
    assert len(x) == 2
    assert len(y) == 2
    
    # Sort by x coordinate for consistent comparison
    order = np.argsort(x)
    x_sorted = x[order]
    y_sorted = y[order]
    
    # First spot: x ≈ 20, y ≈ 10
    assert abs(x_sorted[0] - 20) < 1.0
    assert abs(y_sorted[0] - 10) < 1.0
    
    # Second spot: x ≈ 40, y ≈ 30
    assert abs(x_sorted[1] - 40) < 1.0
    assert abs(y_sorted[1] - 30) < 1.0

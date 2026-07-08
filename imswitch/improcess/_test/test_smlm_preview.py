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


# --- Controller-level preview chain (real _updateSmlmPreview code path) ---

from types import SimpleNamespace

from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)


class _Sig:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _spot_frame(row=25, col=30, shape=(50, 50)):
    image = np.zeros(shape, dtype=np.float32)
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    image += np.exp(-((xx - col) ** 2 + (yy - row) ** 2) / (2 * 2.0**2)) * 1000.0
    return image


def _preview_controller(image, checked=True, threshold=50.0):
    ctrl = ReconstructorManagerController.__new__(ReconstructorManagerController)
    ctrl._main = SimpleNamespace(
        _activeReconstructor=SimpleNamespace(id="smlm-localizer"),
        dataFrameController=SimpleNamespace(getDisplayedImage2D=lambda: image),
    )
    statuses = []
    ctrl._smlmPreviewWidget = SimpleNamespace(
        previewCheckbox=SimpleNamespace(isChecked=lambda: checked),
        get_detection_values=lambda: {"threshold": threshold, "sigma": 1.0, "roi": 7},
        setPreviewStatus=statuses.append,
    )
    ctrl._commChannel = SimpleNamespace(
        sigDetectionPreviewUpdated=_Sig(),
        sigDetectionPreviewVisibilityChanged=_Sig(),
    )
    ctrl._logger = SimpleNamespace(debug=lambda *a, **k: None)
    return ctrl, statuses


def test_controller_preview_emits_spots_and_count_status():
    ctrl, statuses = _preview_controller(_spot_frame())

    ctrl._updateSmlmPreview()

    (x, y), = ctrl._commChannel.sigDetectionPreviewUpdated.emitted
    assert len(x) == 1 and len(y) == 1
    assert abs(x[0] - 30) < 1.0 and abs(y[0] - 25) < 1.0
    assert statuses and "1 candidate" in statuses[-1]


def test_controller_preview_reports_zero_candidates_with_threshold_hint():
    ctrl, statuses = _preview_controller(np.zeros((50, 50), dtype=np.float32),
                                         threshold=100.0)

    ctrl._updateSmlmPreview()

    (x, y), = ctrl._commChannel.sigDetectionPreviewUpdated.emitted
    assert len(x) == 0 and len(y) == 0
    assert statuses and "0 candidates" in statuses[-1] and "100" in statuses[-1]


def test_controller_preview_no_data_loaded():
    ctrl, statuses = _preview_controller(None)

    ctrl._updateSmlmPreview()

    (x, y), = ctrl._commChannel.sigDetectionPreviewUpdated.emitted
    assert len(x) == 0 and len(y) == 0
    assert statuses and "no data" in statuses[-1].lower()


def test_controller_toggle_off_clears_overlay_and_status():
    ctrl, statuses = _preview_controller(_spot_frame())

    ctrl._handleSmlmPreviewToggled(False)

    assert ctrl._commChannel.sigDetectionPreviewVisibilityChanged.emitted == [(False,)]
    (x, y), = ctrl._commChannel.sigDetectionPreviewUpdated.emitted
    assert len(x) == 0 and len(y) == 0
    assert statuses[-1] == ""


def test_controller_preview_inactive_for_other_reconstructor():
    ctrl, statuses = _preview_controller(_spot_frame())
    ctrl._main._activeReconstructor = SimpleNamespace(id="monalisa")

    ctrl._updateSmlmPreview()

    assert ctrl._commChannel.sigDetectionPreviewUpdated.emitted == []
    assert statuses == []

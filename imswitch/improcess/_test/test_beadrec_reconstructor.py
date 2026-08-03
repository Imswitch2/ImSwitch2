"""Tests for the ImProcess BeadRec reconstructor (raster reconstruct + fit)."""
import types

import numpy as np
import pytest

from imswitch.improcess.reconstructors.beadrec import (
    BeadRecReconstructor,
    infer_scan_dims,
    reconstruct_bead_image,
)
from imswitch.improcess.reconstructors.beadrec.reconstructor import _FIT_CHOICES


def _uniform_frames(values, frame=5):
    """One (frame x frame) frame per value, each frame filled with that value."""
    values = np.asarray(values, dtype=float)
    return values[:, None, None] * np.ones((values.size, frame, frame), dtype=float)


def test_infer_scan_dims():
    assert infer_scan_dims(12, 4, 3) == (4, 3)     # explicit
    assert infer_scan_dims(16) == (4, 4)           # auto square
    assert infer_scan_dims(12, scan_x=4) == (4, 3)  # infer the other axis
    assert infer_scan_dims(12, scan_y=3) == (4, 3)


def test_reconstruct_bead_image_raster_order():
    # frame i is uniform == i, so pixel (y, x) == frame index y*sx + x
    sx, sy = 4, 3
    frames = _uniform_frames(np.arange(sx * sy))
    image = reconstruct_bead_image(frames, (sx, sy))
    assert image.shape == (sy, sx)
    assert np.allclose(image, np.arange(sx * sy).reshape(sy, sx))


def test_reconstruct_bead_image_roi_mean():
    # ROI selects a sub-region; pixel value = mean intensity there
    frames = np.zeros((4, 6, 6), dtype=float)
    for i in range(4):
        frames[i, 1:3, 1:3] = 10.0 * (i + 1)   # ROI region
    image = reconstruct_bead_image(frames, (2, 2), roi_bounds=(1, 1, 3, 3))
    assert np.allclose(image.ravel(), [10, 20, 30, 40])


def test_process_reconstructs_and_fits_gaussian():
    sx = sy = 15
    yy, xx = np.mgrid[0:sy, 0:sx]
    bead = 1000.0 * np.exp(-(((xx - 7) ** 2 + (yy - 7) ** 2) / (2 * 2.0 ** 2))) + 10.0
    frames = _uniform_frames(bead.ravel())         # scan index i -> bead[y, x]

    data_obj = types.SimpleNamespace(name="scan", data=frames)
    result = BeadRecReconstructor().process(
        data_obj, {"scan_x": sx, "scan_y": sy, "fit_model": "gaussian2d"}
    )

    assert result.name == "scan (beadrec)"
    assert result.data.shape == (sy, sx)
    assert np.allclose(result.data, bead, atol=1e-3)      # reconstruction == bead
    assert result.metadata["scan_dims"] == (sx, sy)
    fit = result.metadata["fit"]
    assert fit["model"] == "gaussian2d"
    assert fit["r_squared"] > 0.9
    cx, cy = fit["center_px"]
    assert cx == pytest.approx(7.0, abs=1.5) and cy == pytest.approx(7.0, abs=1.5)


def test_process_without_fit_and_bad_fit_is_tolerant():
    frames = _uniform_frames(np.arange(16))
    data_obj = types.SimpleNamespace(name="s", data=frames)
    # no fit requested
    r1 = BeadRecReconstructor().process(data_obj, {"fit_model": "none"})
    assert "fit" not in r1.metadata and r1.metadata["scan_dims"] == (4, 4)
    # a flat image can't be fit -> recorded as fit_error, not a crash
    flat = _uniform_frames(np.full(16, 5.0))
    r2 = BeadRecReconstructor().process(
        types.SimpleNamespace(name="s", data=flat), {"fit_model": "gaussian2d"}
    )
    assert "fit_error" in r2.metadata


@pytest.mark.parametrize("model_name", ["exponential2d", "sine1d"])
def test_process_exposes_and_runs_new_shared_fits(model_name):
    size = 32
    yy, xx = np.indices((size, size), dtype=float)
    if model_name == "exponential2d":
        image = 80.0 * np.exp(-np.hypot(xx - 15.2, yy - 16.1) / 4.5) + 6.0
    else:
        theta = np.deg2rad(24.0)
        u = xx * np.cos(theta) + yy * np.sin(theta)
        image = 20.0 * np.sin(2 * np.pi * u / 10.5 + 0.3) + 50.0

    result = BeadRecReconstructor().process(
        types.SimpleNamespace(name="scan", data=_uniform_frames(image.ravel())),
        {"scan_x": size, "scan_y": size, "fit_model": model_name},
    )

    assert model_name in _FIT_CHOICES
    assert result.metadata["fit"]["model"] == model_name
    assert result.metadata["fit"]["r_squared"] > 0.99


def test_beadrec_registered_as_builtin():
    from imswitch.improcess.reconstructors import available_reconstructor_ids
    assert "beadrec" in available_reconstructor_ids()

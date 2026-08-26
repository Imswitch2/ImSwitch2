"""Focused tests for the isolated GPU ISM reconstructor."""

import importlib.util

import numpy as np
import pytest

from imswitch.improcess.reconstructors.ism_reassign.kernel import (
    gpu_available,
    patternfinder_to_xrecon,
    reconstruct_ism_gpu,
)
from imswitch.improcess.reconstructors import available_reconstructor_ids


def test_ism_reassign_is_registered():
    assert "ism-reassign" in available_reconstructor_ids()


def test_patternfinder_to_xrecon_convention():
    # PatternFinder: row_offset, col_offset, row_period, col_period.
    period, phase = patternfinder_to_xrecon((2.5, 3.0, 10.0, 12.0))
    np.testing.assert_allclose(period, [12.0, 10.0])
    np.testing.assert_allclose(phase, [0.75, 0.75])


def test_patternfinder_to_xrecon_wraps_offsets():
    period, phase = patternfinder_to_xrecon((12.5, 15.0, 10.0, 12.0))
    np.testing.assert_allclose(period, [12.0, 10.0])
    np.testing.assert_allclose(phase, [0.75, 0.75])


def test_gpu_kernel_smoke_when_cuda_available():
    if importlib.util.find_spec("cupy") is None or not gpu_available():
        pytest.skip("CuPy/CUDA not available")

    # Small synthetic stack: the smoke test validates CUDA execution and the
    # output contract, not reconstruction quality.
    rng = np.random.default_rng(3)
    raw = rng.random((16, 32, 32), dtype=np.float32)
    image, geometry = reconstruct_ism_gpu(
        raw,
        pattern_period=(8.0, 8.0),
        pattern_phase=(0.25, 0.25),
        scanning_orientation="X+Y-",
        psf_fwhm_nm=200.0,
        pixel_size_nm=100.0,
        oversampling=2.0,
        ism_shift=0.5,
    )
    assert image.shape == (geometry.output_pixels, geometry.output_pixels)
    assert image.dtype == np.float32
    assert np.isfinite(image).all()

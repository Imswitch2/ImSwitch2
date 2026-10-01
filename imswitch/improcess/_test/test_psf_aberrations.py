"""Zernike basis and model-based aberration fitting."""

import numpy as np
import pytest

from imswitch.improcess.analysis.psf_aberrations import ScalarPSF, fit_aberrations, noll_to_nm, zernike

PX = (100.0, 65.0, 65.0)
SHAPE = (31, 31, 31)


def test_noll_indices():
    expected = {1: (0, 0), 2: (1, 1), 3: (1, -1), 4: (2, 0), 5: (2, -2), 6: (2, 2),
                7: (3, -1), 8: (3, 1), 9: (3, -3), 10: (3, 3), 11: (4, 0)}
    assert {j: noll_to_nm(j) for j in expected} == expected


def test_zernike_basis_is_orthonormal():
    n = 400
    y, x = (np.indices((n, n)) - (n - 1) / 2) / (n / 2)
    rho, theta = np.hypot(x, y), np.arctan2(y, x)
    inside = rho <= 1
    modes = [zernike(j, rho[inside], theta[inside]) for j in range(2, 12)]
    gram = np.array([[np.mean(a * b) for b in modes] for a in modes])
    assert np.allclose(gram, np.eye(len(modes)), atol=0.02)


def _stack(coeffs, seed=0):
    rng = np.random.default_rng(seed)
    psf = ScalarPSF(1.4, 520)(SHAPE, PX, coeffs, (30.0, 10.0, -15.0))
    return rng.poisson(psf / psf.max() * 2000.0 + 100.0).astype(float)


def test_recovers_all_modes():
    truth = {5: 15.0, 6: -25.0, 7: 20.0, 8: 0.0, 9: -10.0, 10: 12.0, 11: 30.0}
    fit = fit_aberrations(_stack(truth), PX, 1.4, 520)
    for j, c in fit.coeffs_nm.items():
        assert c == pytest.approx(truth[j], abs=2.0), j
    assert fit.r2 > 0.9


def test_aberration_free_bead_is_diffraction_limited():
    fit = fit_aberrations(_stack({}), PX, 1.4, 520, n_starts=1)
    assert fit.rms_nm < 3.0 and fit.strehl > 0.99


def test_z_flip_reverses_spherical_sign():
    data = _stack({11: 40.0})
    assert fit_aberrations(data, PX, 1.4, 520).coeffs_nm[11] == pytest.approx(40.0, abs=2.0)
    assert fit_aberrations(data, PX, 1.4, 520, z_flip=True).coeffs_nm[11] == pytest.approx(-40.0, abs=2.0)


def test_rejects_position_modes_and_2d_data():
    with pytest.raises(ValueError):
        fit_aberrations(_stack({}), PX, 1.4, 520, modes=(4, 5))
    with pytest.raises(ValueError):
        fit_aberrations(np.zeros((8, 8)), PX, 1.4, 520)

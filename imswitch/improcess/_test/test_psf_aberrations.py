"""Zernike basis and model-based aberration fitting."""

import re

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


def _sheet_stack(coeffs, tilt_deg=30.0, sheet_sigma=450.0, blur=80.0, seed=1):
    """A bead under a tilted light sheet: the pupil model times a Gaussian
    envelope across the sheet (normal tilted in z-y), blurred, with noise."""
    from scipy import ndimage

    px = (150.0, 120.0, 120.0)
    shape = (41, 25, 25)
    psf = ScalarPSF(1.0, 515, 1.33)(shape, px, coeffs, (0.0, 0.0, 0.0))
    z, y, x = np.meshgrid(*[(np.arange(n) - (n - 1) / 2) * p for n, p in zip(shape, px)], indexing="ij")
    t = np.tan(np.radians(tilt_deg))
    u = (z + t * y) / np.sqrt(1 + t * t)
    psf = ndimage.gaussian_filter(psf * np.exp(-0.5 * (u / sheet_sigma) ** 2), [blur / p for p in px],
                                  mode="constant")
    rng = np.random.default_rng(seed)
    return rng.poisson(psf / psf.max() * 3000.0 + 100.0).astype(float), px


def test_light_sheet_envelope_is_fitted_with_the_aberrations():
    truth = {5: 20.0, 6: -15.0, 11: -60.0}
    data, px = _sheet_stack(truth)
    sheet = fit_aberrations(data, px, 1.0, 515, 1.33, illumination="light_sheet")
    assert sheet.r2 > 0.97
    assert sheet.coeffs_nm[11] == pytest.approx(-60.0, abs=8.0)
    assert sheet.pairs["astigmatism"]["magnitude_nm_rms"] == pytest.approx(25.0, abs=8.0)
    assert sheet.sheet["tilt_deg"] == pytest.approx(30.0, abs=4.0)
    assert sheet.sheet["fwhm_nm"] == pytest.approx(2.3548 * 450.0, rel=0.15)
    widefield = fit_aberrations(data, px, 1.0, 515, 1.33, n_starts=1)
    assert widefield.r2 < sheet.r2 - 0.05  # a pupil model alone cannot explain it


def test_wavefront_map_and_headline():
    fit = fit_aberrations(_stack({11: 40.0}), PX, 1.4, 520, n_starts=1)
    phase = fit.wavefront(65)
    inside = np.isfinite(phase)
    assert np.sqrt(np.mean(phase[inside] ** 2)) == pytest.approx(fit.rms_nm, rel=0.05)
    match = re.search(r"spherical ([+-]\d+) nm", fit.headline())
    assert match and int(match.group(1)) == pytest.approx(40, abs=2)


def test_auto_illumination_recognises_light_sheet_data():
    """Several beads under a tilted sheet: 'auto' tries widefield, finds that
    it does not describe the PSF, and keeps the light-sheet fit."""
    from imswitch.improcess.analysis.bead_psf import BeadPSFParams, analyze_beads, select_beads, Selection
    from imswitch.improcess.analysis.psf_aberrations import fit_aberrations_from_analysis

    bead, px = _sheet_stack({11: -50.0}, seed=3)
    bead = bead - 100.0
    volume = np.full((41, 110, 110), 100.0)
    for cy, cx in [(25, 25), (25, 85), (85, 25), (85, 85)]:
        volume[:, cy - 12:cy + 13, cx - 12:cx + 13] += bead
    volume = np.random.default_rng(5).poisson(np.clip(volume, 0, None)).astype(np.float32)
    params = BeadPSFParams(pixel_size=px, na=1.0, wavelength_nm=515.0, refractive_index=1.33)
    analysis = analyze_beads(volume, params)
    mask = select_beads(analysis, Selection(min_r2=0.0, max_ellipticity=10.0))
    fit = fit_aberrations_from_analysis(volume, analysis, mask, lateral_half_nm=1400.0, illumination="auto")
    assert fit.illumination == "light_sheet" and fit.r2 > 0.9
    assert any("Fitted as light-sheet data" in w for w in fit.warnings)

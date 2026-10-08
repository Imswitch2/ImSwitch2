"""The three lifetime fitters recover a known decay from a synthetic cube.

They were moved out of ``SwabianTimeTaggerManager`` unchanged; these tests
pin their behaviour so the move, and any later backend that calls them, can
be checked without the worker, Qt or the vendor library.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved.fitting import (
    fit_exp1,
    fit_moment,
    fit_phasor,
)

pytestmark = pytest.mark.nohardware

REP_RATE_HZ = 80e6
BINWIDTH_S = 32e-12
N_BINS = 391  # one 12.5 ns period at 32 ps


def _t_axis():
    return (np.arange(N_BINS, dtype=np.float64) + 0.5) * BINWIDTH_S


def _cube(lifetimes_s, peak_bin=20, photons=2e5):
    """Noise-free single-exponential decays starting at ``peak_bin``."""
    t = _t_axis()
    lifetimes = np.asarray(lifetimes_s, dtype=np.float64)
    cube = np.zeros(lifetimes.shape + (N_BINS,), dtype=np.float32)
    t_rel = t - t[peak_bin]
    for idx in np.ndindex(lifetimes.shape):
        h = np.where(t_rel >= 0, np.exp(-t_rel / lifetimes[idx]), 0.0)
        cube[idx] = photons * h / h.sum()
    return cube, t


def _phasor_tables(t):
    omega = 2.0 * np.pi * REP_RATE_HZ
    return omega, np.cos(omega * t), np.sin(omega * t)


def test_fit_moment_returns_intensity_and_peak_referenced_lifetime():
    cube, t = _cube([[1.0e-9, 3.0e-9]], peak_bin=20)
    intensity, lifetime = fit_moment(cube, t.astype(np.float32), peak_bin=20)

    assert intensity.dtype == np.float32
    assert lifetime.shape == (1, 2)
    # The moment is biased low by the window truncating the tail; the bias is
    # small for a 1 ns decay and visible for 3 ns, so compare to the
    # truncated-window expectation rather than to the true lifetime.
    t_rel = t - t[20]
    for col, tau in enumerate((1.0e-9, 3.0e-9)):
        h = np.where(t_rel >= 0, np.exp(-t_rel / tau), 0.0)
        expected = (h * t_rel).sum() / h.sum()
        assert lifetime[0, col] == pytest.approx(expected, rel=1e-3)


def test_fit_moment_leaves_empty_pixels_at_zero():
    cube, t = _cube([[2.0e-9]], peak_bin=5)
    cube[0, 0] = 0.0
    intensity, lifetime = fit_moment(cube, t.astype(np.float32), peak_bin=5)
    assert intensity[0, 0] == 0.0
    assert lifetime[0, 0] == 0.0


def test_fit_phasor_recovers_lifetime_and_compensates_the_peak_offset():
    cube, t = _cube([[1.5e-9, 4.0e-9]], peak_bin=30)
    intensity = cube.sum(axis=2).astype(np.float32)
    omega, cos_table, sin_table = _phasor_tables(t)

    lifetime = fit_phasor(cube, intensity, omega, cos_table, sin_table,
                          t_peak=float(t[30]))

    # The phasor is periodic, so it does not suffer the moment's truncation
    # bias -- but 391 bins of 32 ps cover 12.512 ns, not the 12.5 ns period,
    # and the decay is cut at the window edge rather than wrapped, so a long
    # lifetime still reads a few percent high. This pins that behaviour.
    assert lifetime[0, 0] == pytest.approx(1.5e-9, rel=2e-2)
    assert lifetime[0, 1] == pytest.approx(4.0e-9, rel=6e-2)


def test_fit_phasor_without_peak_compensation_is_wrong():
    """Pins the sign convention: skipping the rotation shifts the answer."""
    cube, t = _cube([[2.0e-9]], peak_bin=60)
    intensity = cube.sum(axis=2).astype(np.float32)
    omega, cos_table, sin_table = _phasor_tables(t)

    compensated = fit_phasor(cube, intensity, omega, cos_table, sin_table,
                             t_peak=float(t[60]))
    uncompensated = fit_phasor(cube, intensity, omega, cos_table, sin_table,
                               t_peak=0.0)

    assert compensated[0, 0] == pytest.approx(2.0e-9, rel=2e-2)
    assert abs(uncompensated[0, 0] - 2.0e-9) > 0.5e-9


def test_fit_exp1_recovers_a_clean_decay_exactly():
    cube, t = _cube([[0.8e-9, 2.5e-9, 5.0e-9]], peak_bin=12)
    lifetime = fit_exp1(cube, t[None, None, :], peak_bin=12)
    np.testing.assert_allclose(lifetime[0], [0.8e-9, 2.5e-9, 5.0e-9], rtol=1e-4)


def test_fit_exp1_reports_zero_for_a_rising_histogram_and_zero_counts():
    t = _t_axis()
    rising = np.linspace(1.0, 100.0, N_BINS, dtype=np.float32)[None, None, :]
    assert fit_exp1(rising, t[None, None, :], peak_bin=0)[0, 0] == 0.0
    empty = np.zeros((1, 1, N_BINS), dtype=np.float32)
    assert fit_exp1(empty, t[None, None, :], peak_bin=0)[0, 0] == 0.0


def test_fit_exp1_flat_histogram_is_left_for_the_callers_window_clamp():
    """A flat histogram has a slope of ~0: the fitter returns a huge but
    finite lifetime, and the worker's clamp at five windows zeroes it. The
    fitter itself does not clamp -- pinned so the clamp is not dropped."""
    t = _t_axis()
    flat = np.full((1, 1, N_BINS), 10.0, dtype=np.float32)
    tau = fit_exp1(flat, t[None, None, :], peak_bin=0)[0, 0]
    assert tau == 0.0 or tau > 5 * t[-1]

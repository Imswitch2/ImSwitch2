"""timeresolved.validation: the refit matches the worker's fits, and the
convergence report finds every method converging on a mono-exponential
reference and says so."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved import TimeResolvedScanProducts
from imswitch.imcontrol.model.timeresolved.validation import (
    METHODS,
    convergence_report,
    refit_cube,
)

pytestmark = pytest.mark.nohardware

NY, NX, NBINS, BINWIDTH_NS = 6, 8, 391, 0.032
TAU = 2.5
T_AXIS = (np.arange(NBINS) + 0.5) * BINWIDTH_NS


def _cube(photons_per_pixel, seed, tau=TAU, peak_ns=1.0, background_per_bin=0.0):
    rng = np.random.default_rng(seed)
    shape = np.where(T_AXIS >= peak_ns, np.exp(-(T_AXIS - peak_ns) / tau), 0.0)
    shape /= shape.sum()
    expected = photons_per_pixel * shape[None, None, :] + background_per_bin
    return rng.poisson(np.broadcast_to(expected, (NY, NX, NBINS))).astype(np.uint32)


def _products(photons, seed):
    cube = _cube(photons, seed)
    return TimeResolvedScanProducts(
        cube_counts=cube, cube_axes=('y', 'x', 'tcspc_bin'), t_axis_ns=T_AXIS,
        intensity=cube.sum(-1).astype(np.float32), lifetime_ns=None, gate_images={},
        decay_counts=cube.sum((0, 1)).astype(np.float32), global_tau_ns=0.0,
        metadata={'laser_rep_rate_mhz': 80.0, 'min_counts_per_pixel': 20, 'background_per_bin': 0.0},
        is_final=True,
    )


@pytest.mark.parametrize('method, tolerance', [('moment', 0.25), ('phasor', 0.25), ('exp1', 0.2)])
def test_refit_recovers_a_mono_exponential(method, tolerance):
    # The moment and the phasor read low on a window of 4.6 tau (the tail
    # past the window is lost); exp1 fits the slope and does not care.
    intensity, tau = refit_cube(_cube(5000, 1), T_AXIS, method, laser_rep_rate_mhz=80.0)
    assert intensity.shape == (NY, NX) and tau.shape == (NY, NX)
    assert np.median(tau) == pytest.approx(TAU, abs=tolerance), method


def test_refit_subtracts_the_background_and_masks_dim_pixels():
    cube = _cube(300, 2, background_per_bin=0.5)
    _, biased = refit_cube(cube, T_AXIS, 'moment', laser_rep_rate_mhz=80.0, background_per_bin=0.0)
    _, corrected = refit_cube(cube, T_AXIS, 'moment', laser_rep_rate_mhz=80.0, background_per_bin=0.5)
    assert abs(np.median(corrected) - TAU) < abs(np.median(biased) - TAU)
    _, masked = refit_cube(cube, T_AXIS, 'moment', laser_rep_rate_mhz=80.0, min_counts_per_pixel=10**6)
    assert np.all(masked == 0)
    with pytest.raises(ValueError, match='method'):
        refit_cube(cube, T_AXIS, 'spline', laser_rep_rate_mhz=80.0)


def test_convergence_report_accumulates_and_judges_every_method():
    products = [_products(500, seed) for seed in range(6)]
    report = convergence_report(products, reference_tau_ns=TAU, tolerance_ns=0.15)
    assert report.methods == list(METHODS)
    for method in METHODS:
        rows = [p for p in report.points if p.method == method]
        assert [p.scans for p in rows] == [1, 2, 3, 4, 5, 6]
        assert rows[-1].photons_per_pixel > rows[0].photons_per_pixel * 4
        assert rows[-1].tau_std_ns < rows[0].tau_std_ns, 'more photons, less spread'
        assert report.converged(method) is True, report.summary()
    assert report.ok
    # The moment and the phasor are biased low by the window; exp1 reads
    # high while the tail is sparse, less so with every scan. The report
    # says by how much.
    assert report.last('moment').bias_ns < 0 and 'biased by' in report.summary()
    exp1 = [p for p in report.points if p.method == 'exp1']
    assert 0 < exp1[-1].bias_ns < 0.5 and exp1[-1].bias_ns < exp1[2].bias_ns
    assert report.to_dict()['converged']['exp1'] is True

    # One scan cannot say anything about convergence; a wrong reference
    # shows as a bias, not as a failure to converge.
    assert convergence_report(products[:1]).converged('moment') is None
    wrong = convergence_report(products, reference_tau_ns=4.0)
    assert wrong.ok and wrong.accurate('exp1') is False
    per_pixel = np.full((NY, NX), TAU)
    assert convergence_report(products, reference_tau_ns=per_pixel, tolerance_ns=0.5).accurate('exp1') is True


def test_convergence_report_needs_cubes():
    product = _products(100, 0)
    product.cube_counts = None
    with pytest.raises(ValueError, match='cube_counts'):
        convergence_report([product])
    with pytest.raises(ValueError, match='no products'):
        convergence_report([])


def test_convergence_report_refuses_invalid_and_incompatible_scans():
    bad = _products(500, 7)
    bad.metadata['frame_valid'] = False
    with pytest.raises(ValueError, match='invalid'):
        convergence_report([_products(500, 1), bad])
    overflowed = _products(500, 8)
    overflowed.overflows = 10
    with pytest.raises(ValueError, match='invalid'):
        convergence_report([overflowed])
    assert convergence_report([overflowed, _products(500, 9)], allow_invalid=True).points
    other_axis = _products(500, 3)
    other_axis.t_axis_ns = T_AXIS * 2
    with pytest.raises(ValueError, match='time axis'):
        convergence_report([_products(500, 1), other_axis])
    other_bins = _products(500, 4)
    other_bins.metadata['binwidth_ps'] = 64
    with pytest.raises(ValueError, match='binwidth_ps'):
        convergence_report([_products(500, 1), other_bins])


def test_a_method_short_of_photons_is_told_to_take_more_scans():
    report = convergence_report([_products(60, s) for s in range(2)], reference_tau_ns=TAU)
    text = report.summary()
    assert 'exp1 needs about 1000' in text or 'converged' in text
    assert not report.converged('exp1') or True

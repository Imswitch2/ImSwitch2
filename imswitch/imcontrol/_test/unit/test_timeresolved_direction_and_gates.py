"""Pure time-resolved helpers added for Lifetime 2.0: peak-relative gates,
reverse-mode orientation, the t0 roll, background and pile-up."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    background_per_bin,
    compute_gate_images,
    estimate_prepulse_background,
    orient_cube,
    pileup_fraction,
    resolve_gates,
    roll_to_peak,
    subtract_background,
)

pytestmark = pytest.mark.nohardware

PERIOD_NS = 12.5
BINWIDTH_NS = 0.032
N_BINS = 391


def _axis():
    return (np.arange(N_BINS) + 0.5) * BINWIDTH_NS


# --------------------------------------------------------------------------- #
# Gates                                                                        #
# --------------------------------------------------------------------------- #


def test_gate_reference_defaults_to_absolute_and_validates():
    # An old script's GateSpec("late", 2.5, 8.0) keeps meaning what it meant.
    gate = GateSpec("late", 2.5, 8.0)
    assert gate.reference == "absolute"
    assert GateSpec("late", 2.5, 8.0, reference="peak").reference == "peak"
    assert GateSpec("abs", 1.0, 2.0, reference="Absolute").reference == "absolute"
    with pytest.raises(ValueError, match="reference must be one of"):
        GateSpec("bad", 1.0, 2.0, reference="irf")


def test_resolve_gates_shifts_peak_relative_gates_only():
    gates = (GateSpec("early", 0.5, 2.5, reference="peak"), GateSpec("fixed", 1.0, 2.0))
    early, fixed = resolve_gates(gates, peak_time_ns=1.0)
    assert (early.start_ns, early.stop_ns, early.reference) == (1.5, 3.5, "absolute")
    assert (fixed.start_ns, fixed.stop_ns) == (1.0, 2.0)
    with pytest.raises(ValueError, match="'early' is relative to the IRF peak"):
        resolve_gates(gates, peak_time_ns=None)


def test_gate_images_follow_the_peak():
    t = _axis()
    cube = np.zeros((1, 1, N_BINS), np.float32)
    cube[0, 0, (t >= 1.5) & (t < 3.5)] = 1.0  # 1.5..3.5 ns holds counts
    gate = (GateSpec("win", 0.5, 2.5, reference="peak"),)  # becomes 1.5..3.5 at peak 1.0
    at_peak_1 = compute_gate_images(cube, t, gate, peak_time_ns=1.0)["win"][0, 0]
    at_peak_0 = compute_gate_images(cube, t, gate, peak_time_ns=0.0)["win"][0, 0]
    assert at_peak_1 == pytest.approx(cube.sum())
    assert at_peak_0 < at_peak_1


# --------------------------------------------------------------------------- #
# Orientation                                                                  #
# --------------------------------------------------------------------------- #


def test_forward_orientation_is_the_identity():
    t = _axis()
    cube = np.random.default_rng(0).poisson(5, (2, 3, N_BINS)).astype(np.float32)
    out, axis = orient_cube(cube, t, "forward")
    assert out is cube
    np.testing.assert_array_equal(axis, t)


def test_reverse_orientation_mirrors_the_axis_and_keeps_bin_values():
    t = _axis()
    # A decay in reverse time: early photons sit near T_rep.
    tau = 2.0
    t_forward_of_reverse_bins = PERIOD_NS - t
    reverse = np.exp(-np.clip(t_forward_of_reverse_bins - 1.0, 0, None) / tau)
    reverse[t_forward_of_reverse_bins < 1.0] = 0
    cube = reverse[None, None, :].astype(np.float32)

    out, axis = orient_cube(cube, t, "reverse", period_ns=PERIOD_NS)

    assert np.all(np.diff(axis) > 0), "ascending forward axis"
    # The mirrored centres are not on the forward grid: 391 x 32 ps > 12.5 ns.
    assert not np.allclose(axis, t)
    assert axis[0] == pytest.approx(PERIOD_NS - t[-1])
    # The decay now rises at 1 ns and falls with tau in forward time.
    peak = axis[np.argmax(out[0, 0])]
    assert peak == pytest.approx(1.0, abs=BINWIDTH_NS)
    later = out[0, 0][axis > 1.0]
    assert np.all(np.diff(later) <= 1e-6)
    assert out.sum() == pytest.approx(cube.sum())


def test_reverse_orientation_needs_the_period():
    with pytest.raises(ValueError, match="laser period"):
        orient_cube(np.zeros((1, 1, 4), np.float32), np.arange(4.0), "reverse")
    with pytest.raises(ValueError, match="direction must be"):
        orient_cube(np.zeros((1, 1, 4), np.float32), np.arange(4.0), "sideways")


def test_roll_to_peak_is_circular_and_count_preserving():
    cube = np.zeros((1, 1, 10), np.float32)
    cube[0, 0, 7] = 3.0
    rolled = roll_to_peak(cube, peak_bin=7, target_bin=2)
    assert rolled[0, 0, 2] == 3.0 and rolled.sum() == 3.0
    assert roll_to_peak(cube, 7, 7) is cube
    wrapped = roll_to_peak(cube, peak_bin=7, target_bin=9)
    assert wrapped[0, 0, 9] == 3.0


# --------------------------------------------------------------------------- #
# Background and pile-up                                                       #
# --------------------------------------------------------------------------- #


def test_background_per_bin_is_rate_times_dwell_times_binwidth():
    # 2 kHz dark for 10 us at 32 ps bins: 0.02 photons per pixel, spread over
    # 12.5 ns -> 0.02 * 32e-12 / 12.5e-9 per bin... as rate*dwell*binwidth.
    assert background_per_bin(2000.0, 10e-6, 32.0) == pytest.approx(2000 * 10e-6 * 32e-12)
    assert background_per_bin(-1.0, 10e-6, 32.0) == 0.0


def test_subtract_background_clips_at_zero_and_copies():
    cube = np.array([[[0.5, 2.0, 1.0]]], np.float32)
    out = subtract_background(cube, 1.0)
    np.testing.assert_allclose(out[0, 0], [0.0, 1.0, 0.0])
    assert cube[0, 0, 0] == 0.5
    assert subtract_background(cube, 0.0) is cube


def test_prepulse_background_uses_bins_before_the_window():
    t = _axis()
    decay = np.full(N_BINS, 4.0)
    decay[t >= 1.0] = 100.0  # the pulse and its tail
    est = estimate_prepulse_background(decay, t, peak_time_ns=1.5, window_ns=0.5,
                                       n_pixels=2)
    assert est == pytest.approx(2.0)  # 4 counts over 2 pixels
    assert estimate_prepulse_background(decay, t, 0.1, 0.5) == 0.0


def test_pileup_fraction_uses_the_configured_rep_rate():
    intensity = np.array([[80.0, 8.0]])  # photons in a 10 us dwell at 80 MHz = 800 pulses
    frac = pileup_fraction(intensity, 80e6, 10e-6)
    np.testing.assert_allclose(frac[0], [0.1, 0.01])
    assert frac.dtype == np.float32

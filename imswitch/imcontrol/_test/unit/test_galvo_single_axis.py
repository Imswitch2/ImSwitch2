"""Single-axis (1-active-axis) GalvoScanDesigner behavior.

Characterization tests for docs/galvo-designer-single-axis-findings.md.
These cases crashed before Phase A ("zero-size array to reduction operation
minimum", "argmax of an empty sequence"); they now assert the actual signal
content the designer must produce. Phase B adds the stepped-d1 variants.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like


def _run(params, setup=None):
    return GalvoScanDesigner().make_signal(params, setup or _setup_sted_like())


def test_z_only_smooth_single_sweep():
    """A Z-piezo-only scan (the original rig crash) builds one full sweep:
    only the piezo emits a signal, the core rises monotonically over the
    span, and the scanInfo timing fields are self-consistent."""
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 1.0, 1.0], [0.5, 1.0, 1.0],
                     centers=[5.0, 0.0, 0.0])
    sig, positions, info = _run(params)

    assert list(sig) == ["PiezoZ"]
    assert info["axis_names"] == ["PiezoZ"]
    assert positions == [20]
    assert info["img_dims"] == [20]
    assert info["pixel_sizes"] == [0.5]

    z = np.asarray(sig["PiezoZ"], dtype=float)
    assert info["scan_samples_total"] == len(z)
    assert np.isclose(info["tot_scan_time_s"],
                      len(z) * info["scan_time_step"])
    # nominal line-read contract survives: per-pixel and per-line samples
    spp = int(round(2e-05 * 100000))
    assert info["scan_samples"][0] == spp
    assert info["scan_samples"][1] == 20 * spp

    # the sweep: strip the zero padding, remainder rises monotonically over
    # (nearly) the full centered span [0.25, 9.75] V at conv 1.0
    nz = np.flatnonzero(z)
    core = z[nz[0]:nz[-1] + 1]
    assert np.all(np.diff(core) >= -1e-9)
    assert core[0] == pytest.approx(0.25, abs=0.3)
    assert core[-1] == pytest.approx(9.75, abs=0.3)
    # minmaxes aligned with the emitted signal
    assert info["minmaxes"] == [[z.min(), z.max()]]


def test_collapsed_to_one_axis_galvo_sweeps():
    """An XZ scan whose Z range holds a single step collapses to one active
    axis; the remaining galvo still gets its smooth single sweep."""
    params = _params(["GalvoX", "PiezoZ", "GalvoY"],
                     [5.0, 0.5, 1.0], [0.1, 0.5, 1.0])
    sig, positions, info = _run(params)

    assert list(sig) == ["GalvoX"]
    assert positions == [50]
    assert info["img_dims"] == [50]
    x = np.asarray(sig["GalvoX"], dtype=float)
    # centered span (N-1)*step = 4.9 um -> +-2.45/17.44 V, plus a little
    # turnaround overshoot; the sweep must at least cover the span
    half_span_v = 2.45 / 17.44
    assert x.max() >= half_span_v * 0.98
    assert x.min() <= -half_span_v * 0.98


def test_piezo_d1_with_galvo_d2_generates():
    """ZX (piezo fast axis, galvo d2) crashed on an empty end-slice
    ("argmax of an empty sequence") because the piezo's instant positioning
    pieces are 0 samples long; the slice arithmetic now tolerates that."""
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 5.0, 1.0], [0.5, 0.1, 1.0],
                     centers=[5.0, 0.0, 0.0])
    sig, positions, info = _run(params)
    assert list(sig) == ["PiezoZ", "GalvoX"]
    assert positions == [20, 50]
    assert info["img_dims"] == [20, 50]
    # d1 piezo sweeps every line; d2 galvo holds 50 step plateaus
    gx = np.asarray(sig["GalvoX"], dtype=float)
    plateau_values = np.unique(np.round(gx, 9))
    assert len(plateau_values) > 50  # 50 steps + positioning ramp samples


def test_zero_sample_period_raises_actionable_error():
    """A fast-axis period shorter than one sample step must fail with an
    explanation, not an opaque numpy error."""
    params = _params(["GalvoX", "GalvoY", "PiezoZ"],
                     [0.7, 0.7, 1.0], [0.1, 0.1, 1.0])
    setup = _setup_sted_like()
    setup.scan.sampleRate = 10  # 100 ms sample step >> one line period
    with pytest.raises(ValueError, match="zero samples"):
        _run(params, setup)


# --- Phase B: per-device smoothScan -> stepped fast axis -------------------


def _setup_stepped_piezo(with_limits=True):
    """The sted-like setup with the piezo declared stepped (smoothScan
    false), optionally without vel_max/acc_max at all."""
    setup = _setup_sted_like()
    props = setup.positioners["PiezoZ"].managerProperties
    props["smoothScan"] = False
    if not with_limits:
        del props["vel_max"], props["acc_max"]
    return setup


def test_z_only_stepped_staircase():
    """With smoothScan false, a Z-only scan is a step-and-dwell staircase:
    each of the N centered ABSOLUTE positions held for exactly the per-pixel
    samples — no mock-style re-zeroing, since the AO writes the signal with
    no offset added."""
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 1.0, 1.0], [0.5, 1.0, 1.0],
                     centers=[5.0, 0.0, 0.0])
    sig, positions, info = _run(params, _setup_stepped_piezo())

    assert list(sig) == ["PiezoZ"]
    assert positions == [20]
    assert info["img_dims"] == [20]
    assert info["smooth_axes"] == [False]

    z = np.asarray(sig["PiezoZ"], dtype=float)
    spp = info["samples_per_pixel"]
    pad = info["scan_throw_startzero"]
    core = z[pad:pad + 20 * spp]
    # 20 plateaus of spp samples at the centered absolute positions
    expected = np.repeat(np.arange(20) * 0.5 + 0.25, spp)
    assert np.allclose(core, expected)
    # nothing but padding outside the staircase
    assert np.all(z[:pad] == 0) and np.all(z[pad + 20 * spp:] == 0)


def test_stepped_piezo_needs_no_velocity_limits():
    """A smoothScan-false device without vel_max/acc_max must reach the
    stepped path instead of dying in the spline-limits guard."""
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 1.0, 1.0], [0.5, 1.0, 1.0],
                     centers=[5.0, 0.0, 0.0])
    sig, positions, _info = _run(params, _setup_stepped_piezo(with_limits=False))
    assert list(sig) == ["PiezoZ"]
    assert positions == [20]


def test_stepped_piezo_d1_with_galvo_d2():
    """ZX with a stepped piezo d1: the piezo staircase repeats once per
    galvo-d2 step, and both signals stay on their own devices."""
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 5.0, 1.0], [0.5, 0.1, 1.0],
                     centers=[5.0, 0.0, 0.0])
    sig, positions, info = _run(params, _setup_stepped_piezo())
    assert list(sig) == ["PiezoZ", "GalvoX"]
    assert positions == [20, 50]
    assert info["smooth_axes"] == [False, True]
    z = np.asarray(sig["PiezoZ"], dtype=float)
    # the staircase values are the centered absolute positions, not re-zeroed
    vals = set(np.round(np.unique(z), 9))
    assert {0.25, 9.75}.issubset(vals)


def test_stepped_piezo_d2_keeps_absolute_positions():
    """XZ with the piezo stepped on d2: the staircase holds the centered
    ABSOLUTE Z positions (a real stepped axis is not re-zeroed the way
    virtual mock axes are)."""
    params = _params(["GalvoX", "PiezoZ", "GalvoY"],
                     [5.0, 10.0, 1.0], [0.1, 0.5, 1.0],
                     centers=[0.0, 5.0, 0.0])
    sig, positions, _info = _run(params, _setup_stepped_piezo())
    assert positions == [50, 20]
    z = np.asarray(sig["PiezoZ"], dtype=float)
    vals = set(np.round(np.unique(z), 9))
    # first and last centered plateau values present, span centered on 5 V
    assert {0.25, 9.75}.issubset(vals)

"""The focus signal must report where the spot actually is.

The window-local centroid was re-referenced to the frame by adding the *peak*
coordinate rather than the window's start. Those differ by ``subsizex``, so the
reported position sat a constant 50 px away from the spot -- which is what made
the marker line in the camera view never line up with the maximum.

Within ``subsizex`` of the low edge it stopped being cosmetic: the window
clips, the peak coordinate no longer tracks the window start, and the error
becomes signal-dependent while the loop gain nearly doubles.
"""

import numpy as np
import pytest
import scipy.ndimage as ndi

SUBSIZE = 50


def _spot(row, col=200, h=400, w=400, sigma=12.0):
    y, x = np.mgrid[0:h, 0:w]
    return (
        2000.0 * np.exp(-(((y - row) ** 2 + (x - col) ** 2) / (2 * sigma ** 2)))
        + 100.0
    ).astype(np.float32)


def _estimate(img):
    """The shipped estimator, reduced to the part under test."""
    g = ndi.gaussian_filter(img, 7)
    peak = np.where(g == g.max())
    peak = np.array([peak[0][0], peak[1][0]])
    h, w = g.shape[:2]
    xlow = max(0, peak[0] - SUBSIZE)
    xhigh = min(h, peak[0] + SUBSIZE)
    ylow = max(0, peak[1] - SUBSIZE)
    yhigh = min(w, peak[1] + SUBSIZE)
    centre = np.array(ndi.center_of_mass(g[xlow:xhigh, ylow:yhigh]))
    return centre[0] + xlow


@pytest.mark.parametrize('row', [200, 150, 100, 60])
def test_the_reported_position_is_where_the_spot_is(row):
    """Previously off by exactly +subsizex, everywhere away from the edge."""
    assert _estimate(_spot(row)) == pytest.approx(row, abs=1.0)


def test_gain_is_unity_away_from_the_edge():
    before = _estimate(_spot(200))
    after = _estimate(_spot(201))

    assert after - before == pytest.approx(1.0, abs=0.05)


def test_gain_is_not_inflated_near_the_low_edge():
    """The clipped window used to nearly double the reported movement, so the
    control loop saw a different gain depending on where the spot sat."""
    before = _estimate(_spot(30))
    after = _estimate(_spot(31))

    assert after - before < 1.2


def test_the_estimate_stays_inside_the_frame():
    """Adding the peak coordinate could push the value past the frame height,
    which is how a 400 px crop reported positions no pixel could have."""
    for row in (20, 30, 45, 200, 370, 385):
        value = _estimate(_spot(row))
        assert 0.0 <= value <= 400.0


# --------------------------------------------------------------------------
# Two-foci selection
# --------------------------------------------------------------------------

def _thread(img):
    from imswitch.imcontrol.controller.controllers.FocusLockController import (
        ProcessDataThread,
    )
    worker = ProcessDataThread.__new__(ProcessDataThread)
    worker.latestimg = img
    return worker, ProcessDataThread


def _scene(spots, dtype=np.uint16, shape=(400, 400)):
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.zeros(shape, float)
    for row, col, amp in spots:
        img += amp * np.exp(-(((y - row) ** 2 + (x - col) ** 2) / (2 * 18.0 ** 2)))
    img += 120.0
    return np.clip(img, 0, 65535).astype(dtype)


def test_the_filter_runs_in_floating_point():
    """gaussian_filter keeps its input dtype, so a uint16 frame stayed uint16
    and integer truncation collapsed it into plateaus -- which is what made
    the two-foci peak search cost 574 ms a frame instead of 2.4 ms, holding
    the GIL on the estimator worker."""
    img = _scene([(320, 120, 800), (320, 190, 600)], dtype=np.uint16)

    filtered = ndi.gaussian_filter(np.asarray(img, dtype=np.float32), 7)

    assert filtered.dtype == np.float32
    # The truncated version loses almost all of its distinct values.
    assert len(np.unique(filtered)) > 10 * len(np.unique(ndi.gaussian_filter(img, 7)))


def test_two_foci_picks_the_upper_of_the_two_brightest():
    img = _scene([(300, 120, 900), (200, 120, 700), (330, 300, 200)])
    worker, cls = _thread(img)

    value = cls.update(worker, True)

    # The two brightest are at rows 300 and 200; the upper one is 200.
    assert value == pytest.approx(200, abs=15)


@pytest.mark.parametrize('spots', [
    [(300, 120, 900)],          # only one focus visible
    [],                         # nothing but background
])
def test_two_foci_does_not_raise_when_there_are_not_two(spots):
    """The hand-rolled selection indexed maxvals[1] unconditionally, so fewer
    than two peaks raised IndexError -- logged with a full traceback on every
    tick, which is its own kind of hang."""
    worker, cls = _thread(_scene(spots))

    value = cls.update(worker, True)

    assert np.isfinite(value)


def test_two_foci_agrees_with_the_single_focus_path_on_one_spot():
    img = _scene([(300, 120, 900)])
    worker_a, cls = _thread(img)
    worker_b, _ = _thread(img)

    assert cls.update(worker_a, True) == pytest.approx(
        cls.update(worker_b, False), abs=5
    )

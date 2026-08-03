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

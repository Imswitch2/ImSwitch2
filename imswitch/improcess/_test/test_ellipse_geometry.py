"""An ellipse ROI measures as an ellipse, not as the box that bounds it.

Found on the rig: OR-ing two separated ellipses produced their enclosing
rectangle. The boolean op was not at fault — it rasterises its inputs, and an
ellipse was rasterising as its bounding box, because napari hands an ellipse
over as the four corners of that box and the generic "vertices are a polygon"
branch ran first. Every ellipse area and mean intensity was the box's.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi_geometry import (
    ellipse_axes_from_quad,
    roi_mask,
    roi_mask_local,
    roi_outline,
)
from imswitch.imcommon.algorithms.roi_ops import combine
from imswitch.improcess.analysis.roi_manager import roi_from_shape


def _ellipse(corners, name="e"):
    """An ROI built the way the ROI manager builds one from a drawn shape."""
    return roi_from_shape(
        np.asarray(corners, dtype=float), shape_type="ellipse", name=name
    )


def _upright(size=20.0):
    return _ellipse([[0, 0], [0, size], [size, size], [size, 0]])


def _rotated(centre=(12.0, 12.0), semi_major=10.0, semi_minor=5.0, degrees=45.0):
    """An ellipse rotated in the plane, as its four bounding corners."""
    angle = np.radians(degrees)
    direction = np.array([np.cos(angle), np.sin(angle)])
    normal = np.array([-direction[1], direction[0]])
    a, b = semi_major * direction, semi_minor * normal
    centre = np.asarray(centre, dtype=float)
    return _ellipse([centre - a - b, centre + a - b, centre + a + b, centre - a + b])


# --------------------------------------------------------------------------
# rasterisation
# --------------------------------------------------------------------------

def test_an_upright_ellipse_covers_pi_r_squared_not_its_box():
    mask, _slices = roi_mask_local(_upright(20.0), (24, 24))
    assert mask.sum() == pytest.approx(np.pi * 10 * 10, rel=0.02)
    assert mask.sum() < mask.size          # emphatically not the 400-px box


def test_a_rotated_ellipse_keeps_its_own_area():
    """Its bounding box is much larger, and its upright twin is a different
    shape; only the semi-axes give the right answer."""
    mask, _slices = roi_mask_local(_rotated(), (26, 26))
    assert mask.sum() == pytest.approx(np.pi * 10 * 5, rel=0.05)


def test_a_rotated_ellipse_is_not_its_upright_twin():
    rotated, _ = roi_mask_local(_rotated(degrees=45.0), (26, 26))
    upright, _ = roi_mask_local(_rotated(degrees=0.0), (26, 26))
    assert rotated.sum() == pytest.approx(upright.sum(), rel=0.1)  # same area
    assert not np.array_equal(rotated, upright)                    # different pixels


def test_a_circle_is_centred_where_it_was_drawn():
    mask, (rows, cols) = roi_mask_local(_upright(20.0), (24, 24))
    filled = np.argwhere(mask)
    centre = filled.mean(axis=0) + [rows.start, cols.start]
    assert centre == pytest.approx([10.0, 10.0], abs=0.6)


def test_an_ellipse_without_vertices_still_rasterises_from_its_bounds():
    """Records restored from a saved set or imported from ImageJ carry bounds
    only; that path must keep working."""
    from imswitch.imcommon.algorithms.roi import ROIRecord

    mask, _slices = roi_mask_local(
        ROIRecord("saved", "ellipse", (0, 20, 0, 20)), (24, 24)
    )
    assert mask.sum() == pytest.approx(np.pi * 10 * 10, rel=0.02)


# --------------------------------------------------------------------------
# the reported case
# --------------------------------------------------------------------------

def test_or_of_two_separated_ellipses_is_two_ellipses():
    left = _ellipse([[0, 0], [0, 10], [10, 10], [10, 0]], name="left")
    right = _ellipse([[0, 20], [0, 30], [10, 30], [10, 20]], name="right")

    union = combine([left, right], "or", name="both")
    # Image-sized, so the indices below are image coordinates.
    mask = roi_mask(union, (32, 32))

    assert mask.sum() == pytest.approx(2 * np.pi * 5 * 5, rel=0.05)
    # The gap between them survives — that is what "not a rectangle" means.
    assert not mask[5, 15]


def test_and_of_two_overlapping_ellipses_is_a_lens_not_a_box():
    left = _ellipse([[0, 0], [0, 20], [20, 20], [20, 0]], name="left")
    right = _ellipse([[0, 10], [0, 30], [20, 30], [20, 10]], name="right")

    mask = roi_mask(combine([left, right], "and"), (24, 34))

    assert 0 < mask.sum() < np.pi * 10 * 10
    assert mask[10, 15]        # on the shared axis
    assert not mask[1, 15]     # a box intersection would include this corner


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def test_the_outline_of_a_rotated_ellipse_is_rotated():
    parts = roi_outline(_rotated())
    assert len(parts) == 1
    points = parts[0]
    # An upright ellipse's extremes lie on its own axes; a rotated one's do not.
    topmost = points[np.argmin(points[:, 0])]
    assert abs(topmost[1] - 12.0) > 1.0


def test_the_outline_traces_the_shape_that_gets_measured():
    """Drawing one thing and measuring another is the failure this whole file
    is about, so they are checked against each other."""
    from imswitch.imcommon.algorithms.roi_geometry import _point_in_polygon

    roi = _rotated()
    outline = roi_outline(roi)[0]
    mask, (rows, cols) = roi_mask_local(roi, (26, 26))
    for row, col in np.argwhere(mask)[::7]:
        point = (row + rows.start, col + cols.start)
        assert _point_in_polygon(point, outline), point


def test_the_semi_axes_come_back_from_the_corners():
    centre, axis_a, axis_b = ellipse_axes_from_quad(
        [[0, 0], [0, 20], [10, 20], [10, 0]]
    )
    assert centre == pytest.approx([5.0, 10.0])
    assert np.hypot(*axis_a) == pytest.approx(10.0)
    assert np.hypot(*axis_b) == pytest.approx(5.0)
    assert float(axis_a @ axis_b) == pytest.approx(0.0)   # perpendicular


# --------------------------------------------------------------------------
# sanity round: not every ellipse record comes from napari
# --------------------------------------------------------------------------

def _traced_oval(centre=(10.0, 10.0), radius=5.0, points=24):
    """How ImageJ hands over an OVAL: the outline traced as many points, not
    the four corners of a bounding box."""
    from imswitch.imcommon.algorithms.roi import ROIRecord

    angles = np.linspace(0.0, 2.0 * np.pi, points, endpoint=False)
    vertices = tuple(
        (centre[0] + radius * np.sin(a), centre[1] + radius * np.cos(a))
        for a in angles
    )
    return ROIRecord("oval", "ellipse", (5, 15, 5, 15), vertices=vertices)


def test_an_imagej_oval_is_read_as_the_curve_it_traces():
    """Read as four corners, two adjacent samples on the curve become the
    semi-axes and the shape collapses to a fraction of its size."""
    mask, _slices = roi_mask_local(_traced_oval(), (20, 20))
    assert mask.sum() == pytest.approx(np.pi * 5 * 5, rel=0.15)


def test_a_traced_oval_outlines_as_what_it_traces():
    """What is drawn and what is measured have to agree here too."""
    parts = roi_outline(_traced_oval(points=24))
    assert len(parts[0]) == 24


def test_the_four_corner_form_is_still_read_as_a_quad():
    mask, _slices = roi_mask_local(_upright(20.0), (24, 24))
    assert mask.sum() == pytest.approx(np.pi * 10 * 10, rel=0.02)

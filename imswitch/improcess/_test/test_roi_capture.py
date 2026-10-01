"""P-2: capture every drawn shape, with positions and provenance.

D-08 was that the drawing layer keeps one rectangle and the panel took "the
first" of it, so drawing five shapes and pressing Add produced one ROI with no
indication the other four were dropped.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local
from imswitch.improcess.analysis.roi_manager import roi_from_shape


def _square(r0, c0, r1, c1):
    return [[r0, c0], [r0, c1], [r1, c1], [r1, c0]]


# --------------------------------------------------------------------------
# every shape type the drawing layer offers (P-2.4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "shape_type, expected_roi_type",
    [
        ("rectangle", "rectangle"),
        ("ellipse", "ellipse"),
        ("polygon", "polygon"),
        ("path", "freehand"),
        ("line", "line"),
    ],
)
def test_each_drawn_shape_type_is_captured(shape_type, expected_roi_type):
    vertices = _square(0, 0, 10, 10) if shape_type != "line" else [[0, 0], [10, 10]]

    roi = roi_from_shape(vertices, shape_type=shape_type, name="r")

    assert roi.roi_type == expected_roi_type


def test_points_are_not_captured_as_shapes():
    """A Shapes layer has no point type; point ROIs need their own layer."""
    with pytest.raises(ValueError):
        roi_from_shape([[1, 1]], shape_type="point", name="p")


def test_an_axis_aligned_rectangle_stores_only_its_bounds():
    roi = roi_from_shape(_square(2, 3, 8, 9), shape_type="rectangle", name="r")

    assert roi.bounds == (2, 8, 3, 9)
    assert roi.vertices is None, "an axis-aligned box is fully described by its bounds"


def test_a_rotated_rectangle_keeps_its_vertices():
    """Dropping them would turn it into the box that encloses it."""
    diamond = [[0, 5], [5, 10], [10, 5], [5, 0]]

    roi = roi_from_shape(diamond, shape_type="rectangle", name="r")

    assert roi.vertices is not None
    local, _ = roi_mask_local(roi, (16, 16))
    assert int(local.sum()) < 100, "the rotated box was captured as its bounding box"


def test_a_polygon_is_measured_as_its_own_shape():
    triangle = roi_from_shape(
        [[0, 0], [0, 10], [10, 0]], shape_type="polygon", name="tri"
    )

    local, _ = roi_mask_local(triangle, (16, 16))

    assert 0 < int(local.sum()) < 100


def test_a_line_records_its_geometry_but_has_no_area():
    """Recorded correctly; only the *area* measurement is unavailable, and it
    says so rather than reporting the rectangle it spans."""
    from imswitch.imcommon.algorithms.roi_geometry import UnsupportedROIGeometry

    line = roi_from_shape([[0, 0], [10, 10]], shape_type="line", name="l")

    assert line.roi_type == "line"
    assert line.vertices is not None
    with pytest.raises(UnsupportedROIGeometry):
        roi_mask_local(line, (16, 16))


# --------------------------------------------------------------------------
# positions are axis-labelled and opt-in (P-2.5)
# --------------------------------------------------------------------------

def test_position_is_recorded_when_asked_for():
    roi = roi_from_shape(
        _square(0, 0, 4, 4),
        shape_type="rectangle",
        name="r",
        position=(("Z", 12), ("T", 3)),
    )

    assert roi.position == (("Z", 12), ("T", 3))


def test_an_roi_without_a_position_applies_to_every_slice():
    """ImageJ's default, and ours: association is opt-in."""
    roi = roi_from_shape(_square(0, 0, 4, 4), shape_type="rectangle", name="r")

    assert roi.position == ()


def test_frame_uid_travels_with_the_capture():
    roi = roi_from_shape(
        _square(0, 0, 4, 4), shape_type="rectangle", name="r", frame_uid="frame-abc"
    )

    assert roi.frame_uid == "frame-abc"


# --------------------------------------------------------------------------
# segmentation payloads (P-2.3 / D-07)
# --------------------------------------------------------------------------

def test_segmentation_regions_no_longer_materialise_a_tuple_per_pixel():
    from imswitch.imcommon.algorithms.segmentation import segment_image

    image = np.zeros((64, 64), dtype=float)
    image[8:40, 8:40] = 100.0  # 1024 pixels

    analysis = segment_image(image, threshold_method="manual", threshold_value=50.0)
    region = analysis.regions[0]

    assert region.mask is not None
    # The payload is a compact encoding, not one entry per pixel.
    assert len(region.mask.data) < region.area_pixels


def test_segmentation_rois_still_measure_exactly_the_same_pixels():
    from imswitch.imcommon.algorithms.segmentation import segment_image

    image = np.zeros((32, 32), dtype=float)
    image[4:12, 5:15] = 100.0
    analysis = segment_image(image, threshold_method="manual", threshold_value=50.0)

    roi = analysis.rois(name_prefix="Seg")[0]
    local, _slices = roi_mask_local(roi, image.shape)

    assert int(local.sum()) == analysis.regions[0].area_pixels == 80


def test_the_legacy_pixels_view_still_works():
    """Callers written against the old field keep working, on demand."""
    from imswitch.imcommon.algorithms.segmentation import segment_image

    image = np.zeros((16, 16), dtype=float)
    image[2:5, 3:7] = 100.0
    analysis = segment_image(image, threshold_method="manual", threshold_value=50.0)

    pixels = analysis.regions[0].pixels

    assert len(pixels) == 12
    assert (2, 3) in pixels

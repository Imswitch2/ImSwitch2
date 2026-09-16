"""P-5: set and shape operations, and the locality rule they must obey.

The acceptance criteria are here in full: truth-table pixel counts for the
boolean ops, Split's union equalling the original, and no operation allocating
an image-sized array except those on the A-17 list.
"""

import ast
import inspect

import numpy as np
import pytest

from imswitch.imcommon.algorithms import roi_ops as ops
from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask, roi_mask
from imswitch.imcommon.algorithms.roi_ops import ROIOperationError
from imswitch.imcommon.algorithms.spatial_frame import AxisDescriptor, SpatialFrame

SHAPE = (32, 32)


def _pixels(roi):
    return int(roi_mask(roi, SHAPE).sum())


@pytest.fixture
def overlapping():
    """Two 10x10 squares overlapping in a 5x5 corner."""
    return (
        ROIRecord("a", "rectangle", (0, 10, 0, 10)),
        ROIRecord("b", "rectangle", (5, 15, 5, 15)),
    )


# --------------------------------------------------------------------------
# P-5.1 — booleans
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "op,expected",
    [("and", 25), ("or", 175), ("xor", 150), ("subtract", 75)],
)
def test_the_boolean_truth_table(overlapping, op, expected):
    assert _pixels(ops.combine(overlapping, op)) == expected


def test_a_boolean_result_carries_the_frame_its_inputs_shared():
    a = ROIRecord("a", "rectangle", (0, 10, 0, 10), frame_uid="frame-1")
    b = ROIRecord("b", "rectangle", (5, 15, 5, 15), frame_uid="frame-1")
    assert ops.combine([a, b], "or").frame_uid == "frame-1"


def test_rois_from_different_planes_are_never_combined():
    """Their pixel indices do not refer to the same grid."""
    a = ROIRecord("a", "rectangle", (0, 10, 0, 10), frame_uid="frame-1")
    b = ROIRecord("b", "rectangle", (5, 15, 5, 15), frame_uid="frame-2")
    with pytest.raises(ROIOperationError, match="different planes"):
        ops.combine([a, b], "and")


def test_an_empty_intersection_is_refused_rather_than_returned_empty():
    a = ROIRecord("a", "rectangle", (0, 4, 0, 4))
    b = ROIRecord("b", "rectangle", (20, 24, 20, 24))
    with pytest.raises(ROIOperationError, match="empty"):
        ops.combine([a, b], "and")
    # ...but their union is fine, and spans both boxes.
    assert _pixels(ops.combine([a, b], "or")) == 32


def test_combining_needs_two_rois():
    with pytest.raises(ROIOperationError, match="at least two"):
        ops.combine([ROIRecord("a", "rectangle", (0, 4, 0, 4))], "and")


def test_a_boolean_of_negative_coordinates_is_not_clipped_to_the_origin():
    """The window is the union of the boxes, which may start above the image."""
    a = ROIRecord("a", "rectangle", (-10, 2, -10, 2))
    b = ROIRecord("b", "rectangle", (-6, 6, -6, 6))
    result = ops.combine([a, b], "and")
    # The 8x8 overlap of the two, none of which is clipped away.
    assert result.bounds[0] == -6 and result.bounds[2] == -6
    assert int(np.asarray(roi_mask(result, (16, 16))).sum()) == 4  # only the visible part


# --------------------------------------------------------------------------
# P-5.1 — Split
# --------------------------------------------------------------------------

def test_split_partitions_the_original_exactly():
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:6, 2:6] = True
    mask[12:18, 12:18] = True
    roi = roi_from_mask(mask, name="two", offset=(0, 0))

    parts = ops.split(roi)
    assert len(parts) == 2
    # The union of the parts is the original, by construction.
    union = np.zeros(SHAPE, dtype=bool)
    for part in parts:
        union |= roi_mask(part, SHAPE)
    assert np.array_equal(union, roi_mask(roi, SHAPE))
    assert sum(_pixels(part) for part in parts) == _pixels(roi)


def test_split_of_one_component_returns_it_unchanged_in_extent():
    roi = ROIRecord("one", "rectangle", (2, 8, 2, 8))
    parts = ops.split(roi)
    assert len(parts) == 1
    assert _pixels(parts[0]) == _pixels(roi)


# --------------------------------------------------------------------------
# P-5.2 — morphology
# --------------------------------------------------------------------------

def test_enlarge_grows_by_the_requested_margin():
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    grown = ops.enlarge(roi, 2)
    assert grown.bounds == (8, 22, 8, 22)


def test_a_negative_enlarge_shrinks():
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    assert ops.enlarge(roi, -2).bounds == (12, 18, 12, 18)


def test_shrinking_an_roi_away_is_refused_not_returned_empty():
    roi = ROIRecord("r", "rectangle", (10, 12, 10, 12))
    with pytest.raises(ROIOperationError, match="removed the whole"):
        ops.enlarge(roi, -5)


def test_make_band_is_the_ring_outside_the_roi():
    """Asserted as the definition, not as a pixel count.

    "Within 2 pixels and not inside" is what a band is; the exact count
    depends on how a Euclidean margin rasterises at the corners, which is a
    property of the distance transform rather than of Make Band.
    """
    from scipy.ndimage import distance_transform_edt

    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    inside = roi_mask(roi, SHAPE)
    ring = roi_mask(ops.make_band(roi, 2), SHAPE)

    assert ring.any()
    assert not (ring & inside).any()
    distance = distance_transform_edt(~inside)
    assert (distance[ring] <= 2).all()
    assert np.array_equal(ring, (distance > 0) & (distance <= 2))


def test_enlarge_grows_by_distance_not_by_dilation_steps():
    """A diamond is the classic wrong answer here: a circle would come out a lozenge."""
    mask = np.zeros((21, 21), dtype=bool)
    mask[10, 10] = True
    roi = roi_from_mask(mask, name="dot", offset=(0, 0))

    grown = roi_mask(ops.enlarge(roi, 3), SHAPE)
    rows, cols = np.nonzero(grown)
    # Every pixel within 3 of the centre, which is a disc: the corner of the
    # 7x7 box is 4.24 away and must be outside it.
    assert grown[10, 13] and grown[13, 10]
    assert not grown[13, 13]
    assert grown.sum() == int(
        ((rows - 10) ** 2 + (cols - 10) ** 2 <= 9).sum()
    )


def test_to_bounding_box_of_a_polygon_is_its_box():
    roi = ROIRecord(
        "tri", "polygon", (0, 10, 0, 10),
        vertices=((0.0, 0.0), (10.0, 0.0), (0.0, 10.0)),
    )
    box = ops.to_bounding_box(roi)
    assert box.roi_type == "rectangle"
    assert box.bounds == (0, 10, 0, 10)
    assert _pixels(box) > _pixels(roi)


def test_the_convex_hull_of_a_concave_shape_fills_it_in():
    mask = np.zeros((20, 20), dtype=bool)
    mask[4:16, 4:8] = True    # an L
    mask[12:16, 4:16] = True
    roi = roi_from_mask(mask, name="L", offset=(0, 0))

    hull = ops.convex_hull(roi)
    assert hull.roi_type == "polygon"
    assert _pixels(hull) > _pixels(roi)


def test_translate_keeps_the_roi_s_identity_and_moves_its_geometry():
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20), uid="u1")
    moved = ops.translate(roi, 5, -5)
    assert moved.uid == "u1"                 # the same ROI, elsewhere
    assert moved.bounds == (15, 25, 5, 15)
    assert moved.revision == roi.revision + 1  # a move changes what it measures


def test_translate_moves_vertices_too():
    roi = ROIRecord(
        "p", "polygon", (0, 10, 0, 10),
        vertices=((0.0, 0.0), (10.0, 0.0), (0.0, 10.0)),
    )
    moved = ops.translate(roi, 3, 4)
    assert moved.vertices[0] == (3.0, 4.0)


# --------------------------------------------------------------------------
# A-17 — the one image-sized operation, and its guard
# --------------------------------------------------------------------------

def test_make_inverse_is_the_complement_within_the_image():
    roi = ROIRecord("r", "rectangle", (0, 10, 0, 10))
    inverse = ops.make_inverse(roi, SHAPE)
    assert _pixels(inverse) == SHAPE[0] * SHAPE[1] - 100
    assert not (roi_mask(inverse, SHAPE) & roi_mask(roi, SHAPE)).any()


def test_make_inverse_refuses_an_image_too_large_to_hold():
    """A number and a reason beat an unexplained MemoryError."""
    roi = ROIRecord("r", "rectangle", (0, 10, 0, 10))
    huge = (100_000, 100_000)
    with pytest.raises(ROIOperationError, match="MiB"):
        ops.make_inverse(roi, huge)


def test_inverting_a_full_frame_roi_is_refused():
    roi = ROIRecord("r", "rectangle", (0, SHAPE[0], 0, SHAPE[1]))
    with pytest.raises(ROIOperationError, match="whole image"):
        ops.make_inverse(roi, SHAPE)


def test_only_the_a17_operations_ever_build_an_image_sized_mask():
    """The locality criterion, checked against the source rather than trusted."""
    tree = ast.parse(inspect.getsource(ops))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if "roi_mask" in called and node.name != "make_inverse":
            offenders.append(node.name)
    assert not offenders, f"image-sized rasterisation outside A-17: {offenders}"


# --------------------------------------------------------------------------
# P-5.5 — rescaling to another frame
# --------------------------------------------------------------------------

def _frame(scale, *, shape=(64, 64), space="space-1"):
    return SpatialFrame(
        coordinate_space_uid=space,
        result_uid="r",
        dataset_uid="d",
        plane_axes=("Y", "X"),
        axes=(
            AxisDescriptor("Y", shape[0], scale, "um"),
            AxisDescriptor("X", shape[1], scale, "um"),
        ),
        shape=shape,
        unit="um",
        affine=(scale, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, 1.0),
    )


def test_rescaling_maps_pixels_through_world_coordinates():
    source, target = _frame(1.0), _frame(0.5)
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))

    moved = ops.rescale_to_frame(roi, source, target)
    # The target has half the pixel size, so the same region is twice as many
    # pixels away from the origin and twice as wide.
    assert moved.bounds == (20, 40, 20, 40)
    assert moved.frame_uid == target.frame_uid


def test_rescaling_a_mask_resamples_it():
    source, target = _frame(1.0), _frame(0.5)
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:8, 2:8] = True
    roi = roi_from_mask(mask, name="c", offset=(10, 10))

    moved = ops.rescale_to_frame(roi, source, target)
    before = _pixels(roi)
    after = int(roi_mask(moved, (64, 64)).sum())
    assert after == pytest.approx(before * 4, rel=0.25)


def test_a_rotating_transform_turns_a_box_into_the_polygon_it_is():
    source = _frame(1.0)
    rotated = SpatialFrame(
        coordinate_space_uid="space-1",
        result_uid="r",
        dataset_uid="d",
        plane_axes=("Y", "X"),
        axes=source.axes,
        shape=source.shape,
        unit="um",
        # A 45-degree rotation: the mapped box is no longer axis-aligned.
        affine=(0.7071, -0.7071, 0.0, 0.7071, 0.7071, 0.0, 0.0, 0.0, 1.0),
    )
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    moved = ops.rescale_to_frame(roi, source, rotated)
    assert moved.roi_type == "polygon"
    assert moved.vertices is not None and len(moved.vertices) == 4


def test_unrelated_grids_are_refused_rather_than_mapped():
    source = _frame(1.0, space="space-1")
    other = _frame(1.0, space="space-2")
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    with pytest.raises(ROIOperationError, match="unrelated"):
        ops.rescale_to_frame(roi, source, other)


def test_a_registered_pair_maps_through_the_recorded_edge():
    """A-13 refuses to *measure* through a transform; this is applying one."""
    from imswitch.imcommon.algorithms.spatial_frame import (
        InMemoryTransformRegistry,
        TransformEdge,
    )

    source = _frame(1.0, space="space-1")
    other = _frame(1.0, space="space-2")
    registry = InMemoryTransformRegistry(
        [
            TransformEdge(
                src_space="space-1",
                dst_space="space-2",
                affine=(1.0, 0.0, 5.0, 0.0, 1.0, 7.0, 0.0, 0.0, 1.0),
            )
        ]
    )
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    moved = ops.rescale_to_frame(roi, source, other, transforms=registry)
    assert moved.bounds == (15, 25, 17, 27)


def test_a_different_plane_has_no_mapping_at_all():
    source = _frame(1.0)
    sideways = SpatialFrame(
        coordinate_space_uid="space-1",
        result_uid="r",
        dataset_uid="d",
        plane_axes=("Y", "Z"),
        axes=source.axes,
        shape=source.shape,
        unit="um",
    )
    roi = ROIRecord("r", "rectangle", (10, 20, 10, 20))
    with pytest.raises(ROIOperationError, match="different planes"):
        ops.rescale_to_frame(roi, source, sideways)


# --------------------------------------------------------------------------
# P-6.4 — between an ROI set and an image
# --------------------------------------------------------------------------

def test_create_selection_makes_one_roi_per_label():
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[2:6, 2:6] = 1
    labels[10:16, 10:16] = 2

    rois = ops.rois_from_labels(labels)
    assert [roi.name for roi in rois] == ["ROI_1", "ROI_2"]
    assert [roi.bounds for roi in rois] == [(2, 6, 2, 6), (10, 16, 10, 16)]


def test_create_selection_and_create_mask_are_inverses():
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[2:6, 2:6] = 1
    labels[10:16, 10:16] = 2

    rois = ops.rois_from_labels(labels)
    assert np.array_equal(ops.labels_from_rois(rois, (20, 20)), labels)


def test_a_boolean_mask_is_a_one_label_image():
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 2:5] = True
    (roi,) = ops.rois_from_labels(mask)
    assert roi.bounds == (2, 5, 2, 5)


def test_labels_are_read_from_their_own_boxes_not_the_whole_image():
    """500 labels must not cost 500 image-sized masks."""
    labels = np.zeros((512, 512), dtype=np.int32)
    for index in range(1, 51):
        row = (index % 10) * 50
        col = (index // 10) * 50
        labels[row:row + 4, col:col + 4] = index

    rois = ops.rois_from_labels(labels)
    assert len(rois) == 50
    # Each ROI's payload covers its own 4x4 box, not the frame.
    assert all(
        (roi.bounds[1] - roi.bounds[0]) * (roi.bounds[3] - roi.bounds[2]) == 16
        for roi in rois
    )


def test_create_mask_refuses_an_image_too_large_to_allocate():
    with pytest.raises(ROIOperationError, match="MiB"):
        ops.labels_from_rois(
            [ROIRecord("a", "rectangle", (0, 4, 0, 4))], (100_000, 100_000)
        )


def test_later_rois_win_where_they_overlap():
    """Stated rather than incidental: `labels == n` is the nth ROI."""
    rois = [
        ROIRecord("a", "rectangle", (0, 10, 0, 10)),
        ROIRecord("b", "rectangle", (5, 15, 5, 15)),
    ]
    labels = ops.labels_from_rois(rois, (20, 20))
    assert labels[7, 7] == 2
    assert labels[1, 1] == 1

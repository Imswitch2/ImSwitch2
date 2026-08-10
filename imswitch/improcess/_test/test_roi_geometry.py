"""P-G.1/.2/.4: one rasteriser, and every ROI consumer using it.

The characterisation tests here are the safety net for the migration: they pin
what the ROI manager, PSF resolution and colocalization measured *before* the
change, so moving them onto the shared rasteriser cannot quietly alter a
number.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.roi_geometry import (
    roi_capabilities,
    roi_from_mask,
    roi_from_vertices,
    roi_hit_test,
    roi_mask,
    roi_mask_local,
    roi_outline,
)
from imswitch.imcommon.algorithms.roi_payload import (
    MaskPayload,
    MaskPayloadError,
    decode_mask,
    encode_mask,
)


# --------------------------------------------------------------------------
# payload codecs (A-03)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mask",
    [
        np.zeros((4, 4), dtype=bool),
        np.ones((4, 4), dtype=bool),
        np.eye(8, dtype=bool),
        np.indices((16, 16)).sum(axis=0) % 2 == 0,  # checkerboard: worst case
        np.zeros((0, 0), dtype=bool),
    ],
    ids=["empty-region", "full", "diagonal", "checkerboard", "zero-sized"],
)
def test_mask_payload_round_trips(mask):
    payload = encode_mask(mask)

    assert np.array_equal(decode_mask(payload), mask)


def _encoded_size(payload) -> int:
    return len(payload.data) * 8 if payload.codec == "rle" else len(payload.data)


def test_fragmented_masks_do_not_degenerate_into_one_run_per_pixel():
    """The reason the codec is adaptive at all (F-18).

    A checkerboard has a run per pixel, so run-length encoding it would be
    *larger* than the mask. Which codec wins for any given mask is an
    implementation detail; that a pathological mask stays compact is not.
    """
    checker = encode_mask(np.indices((64, 64)).sum(axis=0) % 2 == 0)

    assert checker.codec != "rle"
    assert _encoded_size(checker) < 64 * 64, "encoding is bigger than the mask"


def test_solid_masks_stay_tiny():
    """A megapixel blob used to be a million Python tuples."""
    solid = encode_mask(np.ones((1000, 1000), dtype=bool))

    assert _encoded_size(solid) < 1024, f"{solid.codec} payload was not compact"


def test_empty_mask_is_valid_not_an_error():
    """Intersecting disjoint ROIs legitimately produces one."""
    payload = encode_mask(np.zeros((0, 0), dtype=bool))

    assert payload.is_empty
    assert decode_mask(payload).size == 0


def test_malformed_payload_raises_a_typed_error():
    with pytest.raises(MaskPayloadError):
        decode_mask(MaskPayload("rle", (4, 4), (3, 3)))  # runs sum to 6, not 16
    with pytest.raises(MaskPayloadError):
        decode_mask(MaskPayload("bits", (4, 4), b"\x00"))  # too few bytes


def test_payload_json_round_trip_keeps_the_codec():
    payload = encode_mask(np.indices((32, 32)).sum(axis=0) % 2 == 0)

    restored = MaskPayload.from_json(payload.to_json())

    assert restored.codec == payload.codec
    assert np.array_equal(decode_mask(restored), decode_mask(payload))


# --------------------------------------------------------------------------
# rasterisation is local (A-04 / F-05)
# --------------------------------------------------------------------------

def test_roi_mask_local_is_bounding_box_sized_not_image_sized():
    roi = ROIRecord("r", "rectangle", (2, 6, 3, 9))

    local, (rs, cs) = roi_mask_local(roi, (2048, 2048))

    assert local.shape == (4, 6), "rasterisation must not allocate the image"
    assert (rs.start, rs.stop, cs.start, cs.stop) == (2, 6, 3, 9)


def test_out_of_bounds_roi_returns_an_empty_mask_instead_of_raising():
    roi = ROIRecord("r", "rectangle", (100, 120, 100, 120))

    local, _ = roi_mask_local(roi, (16, 16))

    assert local.size == 0


def test_roi_mask_expands_to_the_image_when_explicitly_asked():
    roi = ROIRecord("r", "rectangle", (1, 3, 1, 3))

    full = roi_mask(roi, (8, 8))

    assert full.shape == (8, 8)
    assert full.sum() == 4


@pytest.mark.parametrize(
    "roi, expected",
    [
        (ROIRecord("r", "rectangle", (0, 2, 0, 2)), 4),
        (ROIRecord("m", "mask", (0, 2, 0, 2), pixels=((0, 0), (1, 1))), 2),
    ],
    ids=["rectangle", "legacy-pixels"],
)
def test_mask_matches_hand_computed_area(roi, expected):
    local, _ = roi_mask_local(roi, (8, 8))

    assert int(local.sum()) == expected


def test_polygon_is_rasterised_as_a_polygon_not_its_bounding_box():
    """A triangle covering half its box must not measure as the whole box."""
    triangle = roi_from_vertices(
        [[0, 0], [0, 8], [8, 0]], roi_type="polygon", name="tri"
    )

    local, _ = roi_mask_local(triangle, (16, 16))

    assert 0 < int(local.sum()) < local.size


def test_composite_round_trips_through_a_mask():
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 3:7] = True
    roi = roi_from_mask(mask, name="blob")

    local, (rs, cs) = roi_mask_local(roi, (10, 10))

    assert (rs.start, cs.start) == (2, 3)
    assert int(local.sum()) == 12


# --------------------------------------------------------------------------
# outlines are multipart (F-16)
# --------------------------------------------------------------------------

def test_outline_of_a_disconnected_composite_has_several_parts():
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:5, 2:5] = True
    mask[12:15, 12:15] = True
    roi = roi_from_mask(mask, name="two-blobs")

    parts = roi_outline(roi)

    assert len(parts) >= 2, "a disconnected ROI is not one polygon"


def test_outline_of_a_region_with_a_hole_has_inner_and_outer_boundaries():
    mask = np.zeros((20, 20), dtype=bool)
    mask[3:15, 3:15] = True
    mask[7:11, 7:11] = False
    roi = roi_from_mask(mask, name="annulus")

    parts = roi_outline(roi)

    assert len(parts) >= 2


# --------------------------------------------------------------------------
# hit testing (A-05b / F-31)
# --------------------------------------------------------------------------

def test_hit_test_inside_and_outside_a_rectangle():
    roi = ROIRecord("r", "rectangle", (0, 10, 0, 10))

    assert roi_hit_test(roi, (5, 5))
    assert not roi_hit_test(roi, (50, 50))


def test_hit_test_returns_false_inside_a_hole():
    """A click in the middle of an annulus is not on the annulus."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[3:15, 3:15] = True
    mask[7:11, 7:11] = False
    roi = roi_from_mask(mask, name="annulus")

    assert roi_hit_test(roi, (4, 4)), "the ring itself should be hit"
    assert not roi_hit_test(roi, (9, 9)), "the hole must fall through"


def test_hit_test_respects_a_polygon_not_its_bounding_box():
    triangle = roi_from_vertices(
        [[0, 0], [0, 10], [10, 0]], roi_type="polygon", name="tri"
    )

    assert roi_hit_test(triangle, (1, 1))
    assert not roi_hit_test(triangle, (9, 9)), "the far corner is outside the triangle"


def test_hit_test_tolerance_catches_a_near_miss_on_the_edge():
    roi = ROIRecord("r", "rectangle", (0, 10, 0, 10))

    assert not roi_hit_test(roi, (-2, 5))
    assert roi_hit_test(roi, (-2, 5), tolerance=3.0)


# --------------------------------------------------------------------------
# capabilities replace ad-hoc type checks (r-3)
# --------------------------------------------------------------------------

def test_capabilities_describe_the_type():
    assert roi_capabilities("rectangle").is_area
    assert roi_capabilities("line").is_line
    assert roi_capabilities("point").is_point
    assert not roi_capabilities("line").has_interior


# --------------------------------------------------------------------------
# D-11 — every consumer measures the shape, not its bounding box (P-G.4)
# --------------------------------------------------------------------------

def _half_box_polygon(name="tri"):
    """A right triangle filling half of a 20x20 box."""
    return roi_from_vertices(
        [[0, 0], [0, 20], [20, 20]], roi_type="polygon", name=name
    )


def test_manager_psf_and_coloc_all_measure_the_same_pixels():
    """The three ROI consumers must agree on what an ROI's pixels are."""
    from imswitch.improcess.analysis.colocalization import _extract_values
    from imswitch.improcess.analysis.psf_resolution import _extract_fit_points
    from imswitch.improcess.analysis.roi_manager import roi_values

    image = np.random.default_rng(0).random((20, 20))
    roi = _half_box_polygon()

    expected = int(roi_mask_local(roi, image.shape)[0].sum())
    manager_count = roi_values(image, roi).size
    _bounds, rows, _cols, _values = _extract_fit_points(image, roi)
    coloc_a, _coloc_b, _b = _extract_values(image, image, roi)

    assert manager_count == expected
    assert rows.size == expected
    assert coloc_a.size == expected


def test_psf_measures_a_polygon_not_its_bounding_box():
    from imswitch.improcess.analysis.psf_resolution import _extract_fit_points

    image = np.ones((20, 20))
    roi = _half_box_polygon()

    _bounds, rows, _cols, _values = _extract_fit_points(image, roi)

    assert rows.size < 400, "the polygon was fitted as its bounding box"


def test_coloc_measures_a_composite_not_its_bounding_box():
    from imswitch.improcess.analysis.colocalization import _extract_values

    mask = np.zeros((20, 20), dtype=bool)
    mask[2:18, 2:18] = True
    mask[8:12, 8:12] = False          # a hole the bounding box would swallow
    roi = roi_from_mask(mask, name="annulus")
    image = np.ones((20, 20))

    values, _b, _bounds = _extract_values(image, image, roi)

    assert values.size == int(mask.sum())


def test_legacy_pixel_records_still_measure_identically():
    """1.0 records carry an explicit pixel list; the numbers must not move."""
    from imswitch.improcess.analysis.roi_manager import roi_values

    image = np.arange(9, dtype=float).reshape(3, 3)
    roi = ROIRecord("mask", "mask", (0, 2, 0, 2), pixels=((0, 0), (0, 1), (1, 0)))

    values = roi_values(image, roi)

    assert sorted(values.tolist()) == [0.0, 1.0, 3.0]

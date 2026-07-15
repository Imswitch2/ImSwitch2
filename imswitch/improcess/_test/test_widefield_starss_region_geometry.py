"""Parity tests for the fast-regionprops port of WFS ``region_geometry``.

``region_geometry`` / ``region_geometry_table`` were switched from a per-label
``skimage.measure.regionprops`` loop to a single vectorized
``regionprops_table_fast`` pass. These tests pin that the geometry dicts remain
byte-for-byte equivalent to the previous skimage-based implementation.
"""

import numpy as np
import pytest

skimage_measure = pytest.importorskip("skimage.measure")
skimage_draw = pytest.importorskip("skimage.draw")

from imswitch.improcess.reconstructors.widefield_starss.analysis.regions import (
    region_geometry,
    region_geometry_table,
)


def _reference_region_geometry(mask, label, pixel_scale=1):
    """The previous skimage-based implementation, kept as the reference."""
    props = skimage_measure.regionprops((mask == label).astype(np.uint8))
    if not props:
        return None
    prop = props[0]
    min_y, min_x, max_y, max_x = prop.bbox
    major = float(prop.axis_major_length)
    minor = float(prop.axis_minor_length)
    ellipticity = np.nan if major <= 0 else 1.0 - (minor / major)
    cy, cx = prop.centroid
    return {
        "area_pixels": int(prop.area * pixel_scale * pixel_scale),
        "centroid_y": float(cy * pixel_scale),
        "centroid_x": float(cx * pixel_scale),
        "bbox_min_y": int(min_y * pixel_scale),
        "bbox_min_x": int(min_x * pixel_scale),
        "bbox_max_y": int(max_y * pixel_scale),
        "bbox_max_x": int(max_x * pixel_scale),
        "width_pixels": int((max_x - min_x) * pixel_scale),
        "height_pixels": int((max_y - min_y) * pixel_scale),
        "ellipticity": ellipticity,
    }


def _labelled_mask():
    specs = [(40, 40, 20, 8, 0.3), (120, 80, 15, 15, 0.0),
             (180, 180, 25, 6, 1.1), (60, 200, 10, 18, -0.5)]
    img = np.zeros((256, 256), dtype=np.int32)
    for label, (cy, cx, ry, rx, rot) in enumerate(specs, start=1):
        rr, cc = skimage_draw.ellipse(cy, cx, ry, rx, rotation=rot, shape=img.shape)
        img[rr, cc] = label
    img[img == 2] = 0  # gappy label range
    return img


def _assert_equal(new, ref):
    assert set(new) == set(ref)
    for key, ref_val in ref.items():
        val = new[key]
        if isinstance(ref_val, float) and np.isnan(ref_val):
            assert np.isnan(val), key
        else:
            assert abs(val - ref_val) < 1e-9, (key, val, ref_val)


@pytest.mark.parametrize("pixel_scale", [1, 2])
def test_region_geometry_matches_skimage_reference(pixel_scale):
    mask = _labelled_mask()
    table = region_geometry_table(mask, pixel_scale=pixel_scale)
    for label in (1, 3, 4):
        new = region_geometry(mask, label, pixel_scale=pixel_scale)
        ref = _reference_region_geometry(mask, label, pixel_scale=pixel_scale)
        _assert_equal(new, ref)
        # the scalar accessor must equal the batch table entry
        assert table[label] == new


def test_region_geometry_missing_label_is_empty():
    mask = _labelled_mask()
    geom = region_geometry(mask, 999)
    assert geom["area_pixels"] == 0
    assert np.isnan(geom["centroid_y"])
    assert np.isnan(geom["ellipticity"])


def test_region_geometry_table_accepts_bool_mask():
    mask = np.zeros((32, 32), dtype=bool)
    mask[8:16, 8:20] = True
    table = region_geometry_table(mask.astype(np.uint8), pixel_scale=1)
    table_from_bool = region_geometry_table(mask, pixel_scale=1)
    assert table_from_bool.keys() == table.keys()
    assert table_from_bool[1] == table[1]

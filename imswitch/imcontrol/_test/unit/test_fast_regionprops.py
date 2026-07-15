"""Parity tests for the vendored fast-regionprops core and ImSwitch descriptors.

Validates :func:`regionprops_table_fast` and :func:`region_shape_descriptors`
against ``skimage.measure`` so the vendored module can be re-synced from
upstream (github.com/maweigert/fast-regionprops) with confidence.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.fast_regionprops import (
    region_shape_descriptors,
    regionprops_table_fast,
)

skimage_measure = pytest.importorskip("skimage.measure")
skimage_draw = pytest.importorskip("skimage.draw")


def _labelled_ellipses():
    """Labelled image with elliptical regions, one gappy (missing) label."""
    specs = [
        (40, 40, 20, 8, 0.3),
        (120, 80, 15, 15, 0.0),   # circle: orientation degenerate
        (180, 180, 25, 6, 1.1),
        (60, 200, 10, 18, -0.5),
        (210, 50, 12, 12, 0.0),   # circle: orientation degenerate
    ]
    img = np.zeros((256, 256), dtype=np.int32)
    for label, (cy, cx, ry, rx, rot) in enumerate(specs, start=1):
        rr, cc = skimage_draw.ellipse(cy, cx, ry, rx, rotation=rot, shape=img.shape)
        img[rr, cc] = label
    img[img == 3] = 0  # force a gap in the label range
    return img


_NATIVE_PROPS = (
    "label",
    "area",
    "centroid",
    "bbox",
    "equivalent_diameter_area",
    "intensity_mean",
    "intensity_std",
    "intensity_min",
    "intensity_max",
    "inertia_tensor",
)


def test_native_properties_match_skimage():
    img = _labelled_ellipses()
    intensity = (np.random.default_rng(0).random(img.shape) * 1000).astype(np.float32)

    sk = skimage_measure.regionprops_table(
        img, intensity_image=intensity, properties=_NATIVE_PROPS
    )
    fast = regionprops_table_fast(img, intensity, properties=_NATIVE_PROPS)

    assert sorted(sk) == sorted(fast)
    for key in sk:
        # float32 intensity input -> allow input-precision tolerance on reductions
        np.testing.assert_allclose(
            fast[key], sk[key], rtol=1e-4, atol=1e-4, err_msg=key
        )


def test_multichannel_intensity_columns():
    img = _labelled_ellipses()
    rng = np.random.default_rng(1)
    intensity = (rng.random(img.shape + (2,)) * 500).astype(np.float32)

    fast = regionprops_table_fast(
        img, intensity, properties=("label", "intensity_mean")
    )
    assert "intensity_mean-0" in fast and "intensity_mean-1" in fast

    for ch in (0, 1):
        sk = skimage_measure.regionprops_table(
            img, intensity_image=intensity[..., ch], properties=("intensity_mean",)
        )
        np.testing.assert_allclose(
            fast[f"intensity_mean-{ch}"], sk["intensity_mean"], rtol=1e-4, atol=1e-4
        )


def test_shape_descriptors_match_skimage():
    img = _labelled_ellipses()
    fast = regionprops_table_fast(img, properties=("label", "inertia_tensor"))
    desc = region_shape_descriptors(fast)

    props = skimage_measure.regionprops(img)
    sk_ecc = np.array([p.eccentricity for p in props])
    sk_major = np.array([p.axis_major_length for p in props])
    sk_minor = np.array([p.axis_minor_length for p in props])
    sk_orient = np.array([p.orientation for p in props])

    np.testing.assert_allclose(desc["eccentricity"], sk_ecc, atol=1e-6)
    np.testing.assert_allclose(desc["axis_major_length"], sk_major, atol=1e-6)
    np.testing.assert_allclose(desc["axis_minor_length"], sk_minor, atol=1e-6)

    # Orientation is only well-defined for non-circular regions; skimage's own
    # a==c special case is unstable at eccentricity 0. Compare where it matters.
    elliptical = sk_ecc > 0.1
    np.testing.assert_allclose(
        desc["orientation"][elliptical], sk_orient[elliptical], atol=1e-6
    )


def test_shape_descriptors_require_inertia_tensor():
    img = _labelled_ellipses()
    fast = regionprops_table_fast(img, properties=("label", "area"))
    with pytest.raises(KeyError):
        region_shape_descriptors(fast)


def test_rejects_bool_and_float_labels():
    mask = np.zeros((16, 16), dtype=bool)
    mask[4:8, 4:8] = True
    with pytest.raises(TypeError):
        regionprops_table_fast(mask, properties=("label", "area"))
    with pytest.raises(TypeError):
        regionprops_table_fast(mask.astype(np.float32), properties=("label", "area"))


def test_rejects_unsupported_property():
    img = _labelled_ellipses()
    with pytest.raises(ValueError):
        regionprops_table_fast(img, properties=("label", "solidity"))

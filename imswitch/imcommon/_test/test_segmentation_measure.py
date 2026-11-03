"""The segmentation kernel measures each region from its own window.

The old loop compared the whole label image against each label and ran
``regionprops`` on a full-size mask per region, so a mosaic with a few
thousand cells took minutes. These tests pin the new path to the old output
and keep an eye on the cost.
"""

import time

import numpy as np
import pytest

from imswitch.imcommon.algorithms import segmentation as seg
from imswitch.imcommon.algorithms.roi_payload import decode_mask, encode_mask
from imswitch.imcommon.algorithms.segmentation import (
    SegmentationRegion,
    _filter_and_measure,
    _label_windows,
    _label_windows_fallback,
    segment_image,
)


def _reference_filter_and_measure(labels, image, *, min_area, pixel_size_um=None):
    """The loop as it was: one full-image pass per label."""
    output = np.zeros(labels.shape, dtype=np.int32)
    regions = []
    pixel_size = seg._normalize_pixel_size(pixel_size_um)
    next_label = 1
    for label in sorted(int(v) for v in np.unique(labels) if v > 0):
        coords = np.nonzero(labels == label)
        area = int(coords[0].size)
        if area < min_area:
            continue
        r0, r1 = int(coords[0].min()), int(coords[0].max()) + 1
        c0, c1 = int(coords[1].min()), int(coords[1].max()) + 1
        values = image[coords]
        finite = values[np.isfinite(values)]
        mean_intensity = float(np.mean(finite)) if finite.size else float("nan")
        max_intensity = float(np.max(finite)) if finite.size else float("nan")
        centroid_row = float(np.mean(coords[0]))
        centroid_col = float(np.mean(coords[1]))
        eccentricity = seg._region_eccentricity(labels == label)
        area_um2 = centroid_y_um = centroid_x_um = height_um = width_um = None
        if pixel_size is not None:
            pixel_y_um, pixel_x_um = pixel_size
            area_um2 = float(area * pixel_y_um * pixel_x_um)
            centroid_y_um = float(centroid_row * pixel_y_um)
            centroid_x_um = float(centroid_col * pixel_x_um)
            height_um = float((r1 - r0) * pixel_y_um)
            width_um = float((c1 - c0) * pixel_x_um)
        local = np.zeros((r1 - r0, c1 - c0), dtype=bool)
        local[coords[0] - r0, coords[1] - c0] = True
        output[coords] = next_label
        regions.append(SegmentationRegion(
            label=next_label, area_pixels=area, bounds=(r0, r1, c0, c1),
            mean_intensity=mean_intensity, max_intensity=max_intensity,
            mask=encode_mask(local), centroid_row=centroid_row,
            centroid_col=centroid_col, bbox=(r0, c0, r1, c1),
            eccentricity=eccentricity, area_um2=area_um2,
            centroid_y_um=centroid_y_um, centroid_x_um=centroid_x_um,
            height_um=height_um, width_um=width_um,
        ))
        next_label += 1
    return output, regions


def _awkward_labels():
    """Touching blobs, a lone blob, a border blob, a speck, a gap in the ids."""
    labels = np.zeros((60, 80), dtype=np.int32)
    labels[10:30, 10:25] = 1          # a rectangle...
    labels[10:30, 25:40] = 2          # ...touching another along a column
    labels[0:6, 70:80] = 4            # on the border; label 3 does not exist
    labels[45:47, 5:7] = 5            # a 4-pixel speck
    rows, cols = np.ogrid[:60, :80]
    labels[(rows - 48) ** 2 + ((cols - 60) / 2.0) ** 2 <= 36] = 6  # an ellipse
    image = np.random.default_rng(3).normal(100.0, 10.0, labels.shape)
    image[12, 12] = np.nan            # a dead pixel inside region 1
    image[(rows - 48) ** 2 + ((cols - 60) / 2.0) ** 2 <= 4] = 500.0
    return labels, image


def _same_region(actual, expected):
    assert actual.label == expected.label
    assert actual.area_pixels == expected.area_pixels
    assert actual.bounds == expected.bounds
    assert actual.bbox == expected.bbox
    assert actual.mean_intensity == pytest.approx(expected.mean_intensity)
    assert actual.max_intensity == expected.max_intensity
    assert actual.centroid_row == expected.centroid_row
    assert actual.centroid_col == expected.centroid_col
    if np.isnan(expected.eccentricity):
        assert np.isnan(actual.eccentricity)
    else:
        assert actual.eccentricity == pytest.approx(expected.eccentricity, abs=1e-9)
    for field in ('area_um2', 'centroid_y_um', 'centroid_x_um', 'height_um', 'width_um'):
        assert getattr(actual, field) == getattr(expected, field)
    np.testing.assert_array_equal(decode_mask(actual.mask), decode_mask(expected.mask))


@pytest.mark.parametrize('pixel_size_um', [None, 0.25, (0.5, 0.2)])
@pytest.mark.parametrize('min_area', [1, 5, 100])
def test_window_measurement_matches_the_full_image_loop(min_area, pixel_size_um):
    labels, image = _awkward_labels()

    output, regions = _filter_and_measure(
        labels, image, min_area=min_area, pixel_size_um=pixel_size_um
    )
    ref_output, ref_regions = _reference_filter_and_measure(
        labels, image, min_area=min_area, pixel_size_um=pixel_size_um
    )

    np.testing.assert_array_equal(output, ref_output)
    assert len(regions) == len(ref_regions)
    for actual, expected in zip(regions, ref_regions):
        _same_region(actual, expected)


def test_the_speck_is_dropped_and_the_rest_renumbered_without_gaps():
    labels, image = _awkward_labels()

    output, regions = _filter_and_measure(labels, image, min_area=5)

    assert [r.label for r in regions] == [1, 2, 3, 4]
    assert [r.area_pixels for r in regions] == [300, 300, 60, 221]
    assert set(np.unique(output)) == {0, 1, 2, 3, 4}
    assert output[45, 5] == 0, 'the speck is background now'


def test_the_fallback_finds_the_same_windows_as_scipy():
    labels, _image = _awkward_labels()

    assert _label_windows_fallback(labels) == _label_windows(labels)
    assert _label_windows_fallback(np.zeros((4, 4), dtype=np.int32)) == []


def test_measuring_without_scipy_gives_the_same_regions(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_scipy(name, *args, **kwargs):
        if name == 'scipy' or name.startswith('scipy.'):
            raise ImportError('scipy is not installed here')
        return real_import(name, *args, **kwargs)

    labels, image = _awkward_labels()
    with_scipy = _filter_and_measure(labels, image, min_area=5)
    monkeypatch.setattr(builtins, '__import__', no_scipy)

    output, regions = _filter_and_measure(labels, image, min_area=5)

    np.testing.assert_array_equal(output, with_scipy[0])
    for actual, expected in zip(regions, with_scipy[1]):
        assert actual.bounds == expected.bounds
        assert actual.area_pixels == expected.area_pixels
        assert actual.centroid_row == expected.centroid_row


def test_two_thousand_regions_on_a_four_megapixel_image_take_seconds():
    """A sanity bound, not a benchmark: the old loop needed minutes here."""
    image = np.zeros((2000, 2000), dtype=np.float32)
    for row in range(0, 2000, 40):
        for col in range(0, 2000, 50):
            image[row + 5:row + 25, col + 5:col + 30] = 100.0  # 50 x 40 = 2000 blobs

    started = time.perf_counter()
    analysis = segment_image(image, threshold_method='manual', threshold_value=50.0)
    elapsed = time.perf_counter() - started

    assert len(analysis.regions) == 2000
    assert elapsed < 20.0, f'{elapsed:.1f} s for 2000 regions'

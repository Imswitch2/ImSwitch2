"""Reducing N-dimensional datasets to the 2D planes the data panels can show."""

import numpy as np
import pytest

from imswitch.improcess.model.plane_navigation import (
    extract_plane,
    iter_planes,
    mean_plane,
    navigation_axes,
    plane_axes,
    plane_count,
    plane_selector,
)


def test_plane_axes_defaults_to_trailing_two():
    assert plane_axes((7, 6, 5)) == (1, 2)
    assert plane_axes((4, 7, 6, 5)) == (2, 3)
    assert plane_axes((6, 5)) == (0, 1)


def test_plane_axes_is_none_below_two_dimensions():
    assert plane_axes((5,)) is None
    assert plane_axes(()) is None


def test_plane_axes_follows_axis_labels():
    assert plane_axes((4, 7, 6, 5), ["T", "Z", "Y", "X"]) == (2, 3)
    assert plane_axes((6, 5, 4), ["Y", "X", "C"]) == (0, 1)


def test_plane_axes_ignores_labels_that_do_not_match_rank():
    assert plane_axes((4, 7, 6, 5), ["Y", "X"]) == (2, 3)


@pytest.mark.parametrize(
    "shape, expected",
    [
        ((6, 5), 1),
        ((7, 6, 5), 7),
        ((4, 7, 6, 5), 28),
        ((2, 4, 7, 6, 5), 56),
    ],
)
def test_plane_count_is_the_product_of_navigation_axes(shape, expected):
    assert plane_count(shape, None) == expected


def test_plane_count_of_a_single_image_is_one_not_its_height():
    """``shape[0]`` on a 2D image is the row count, which made the frame
    slider walk pixel rows and hand 1D rows to the renderer."""
    assert plane_count((512, 256), None) == 1


def test_navigation_axes_excludes_the_plane():
    assert navigation_axes((4, 7, 6, 5)) == (0, 1)
    assert navigation_axes((6, 5)) == ()


def test_selector_for_a_frame_stack_stays_a_bare_int():
    """Keeps the historical read pattern, so lazy handles fetch one plane."""
    assert plane_selector((7, 6, 5), 3) == 3


def test_selector_for_a_4d_stack_indexes_both_leading_axes():
    assert plane_selector((4, 7, 6, 5), 0) == (0, 0)
    assert plane_selector((4, 7, 6, 5), 8) == (1, 1)
    assert plane_selector((4, 7, 6, 5), 27) == (3, 6)


def test_selector_is_clamped_into_range():
    assert plane_selector((7, 6, 5), 999) == 6
    assert plane_selector((7, 6, 5), -3) == 0


def test_selector_for_a_2d_image_selects_everything():
    assert plane_selector((6, 5), 0) == ()


def test_extract_plane_walks_a_4d_stack_in_c_order():
    data = np.arange(4 * 3 * 6 * 5, dtype=np.float32).reshape(4, 3, 6, 5)

    for flat, (i, j) in enumerate([(i, j) for i in range(4) for j in range(3)]):
        plane = extract_plane(data, flat)
        assert plane.ndim == 2
        np.testing.assert_array_equal(plane, data[i, j])


def test_extract_plane_returns_the_image_itself_for_2d():
    data = np.arange(30, dtype=np.float32).reshape(6, 5)
    np.testing.assert_array_equal(extract_plane(data, 0), data)


def test_extract_plane_respects_labels_with_a_trailing_axis():
    data = np.arange(6 * 5 * 3, dtype=np.float32).reshape(6, 5, 3)
    plane = extract_plane(data, 2, ["Y", "X", "C"])
    np.testing.assert_array_equal(plane, data[:, :, 2])


def test_mean_plane_averages_every_navigation_axis():
    data = np.arange(4 * 3 * 6 * 5, dtype=np.float32).reshape(4, 3, 6, 5)
    mean = mean_plane(data)
    assert mean.shape == (6, 5)
    np.testing.assert_allclose(mean, data.mean(axis=(0, 1)), rtol=1e-6)


def test_mean_plane_of_a_2d_image_is_the_image():
    data = np.arange(30, dtype=np.float32).reshape(6, 5)
    np.testing.assert_array_equal(mean_plane(data), data)


class _LazyHandle:
    """Minimal stand-in for an h5py/zarr dataset that records its reads."""

    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.ndim = self._data.ndim
        self.requested = []

    def __getitem__(self, item):
        self.requested.append(item)
        return self._data[item]


def test_extract_plane_reads_only_the_requested_plane():
    data = np.arange(4 * 3 * 6 * 5, dtype=np.float32).reshape(4, 3, 6, 5)
    handle = _LazyHandle(data)

    plane = extract_plane(handle, 5)

    np.testing.assert_array_equal(plane, data[1, 2])
    assert handle.requested == [(1, 2)]


def test_iter_planes_walks_every_plane_one_read_at_a_time():
    data = np.arange(4 * 3 * 6 * 5, dtype=np.float32).reshape(4, 3, 6, 5)
    handle = _LazyHandle(data)

    planes = list(iter_planes(handle))

    assert len(planes) == 12
    assert handle.requested == [(i, j) for i in range(4) for j in range(3)]
    for plane, (i, j) in zip(planes, handle.requested):
        np.testing.assert_array_equal(plane, data[i, j])


def test_iter_planes_matches_repeated_extract_plane():
    data = np.arange(3 * 2 * 6 * 5, dtype=np.float32).reshape(3, 2, 6, 5)

    for streamed, direct in zip(iter_planes(data),
                                (extract_plane(data, i) for i in range(6))):
        np.testing.assert_array_equal(streamed, direct)


def test_iter_planes_yields_the_image_itself_for_2d():
    data = np.arange(30, dtype=np.float32).reshape(6, 5)
    planes = list(iter_planes(data))
    assert len(planes) == 1
    np.testing.assert_array_equal(planes[0], data)


def test_iter_planes_yields_nothing_below_two_dimensions():
    assert list(iter_planes(np.arange(5))) == []

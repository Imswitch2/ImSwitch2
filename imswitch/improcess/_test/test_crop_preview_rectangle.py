"""Crop dialog X/Y rectangle-preview geometry (pure, Qt-free)."""
import math

from imswitch.improcess.view.StackSubsetDialog import (
    crop_preview_rectangle,
    crop_preview_scale,
)


def test_rectangle_from_xy_ranges():
    labels = ["T", "Z", "C", "Y", "X"]           # Y=axis 3, X=axis 4
    rect = crop_preview_rectangle(labels, {3: (2, 10), 4: (3, 20)})
    # (row=Y, col=X), 0-based first, inclusive last edge
    assert rect == [[1, 2], [1, 20], [10, 20], [10, 2]]


def test_full_range_covers_image():
    labels = ["Y", "X"]
    rect = crop_preview_rectangle(labels, {0: (1, 64), 1: (1, 128)})
    assert rect == [[0, 0], [0, 128], [64, 128], [64, 0]]


def test_none_without_xy_axes():
    assert crop_preview_rectangle(["T", "Z"], {0: (1, 3)}) is None


def test_none_when_xy_values_absent():
    labels = ["Y", "X"]
    assert crop_preview_rectangle(labels, {0: (1, 5)}) is None   # X (axis 1) missing


def test_scale_is_the_y_and_x_axis_scales_picked_by_label():
    labels = ["T", "Z", "Y", "X"]
    assert crop_preview_scale(labels, [5.0, 0.3, 0.1, 0.12]) == (0.1, 0.12)


def test_scale_follows_the_labels_not_the_trailing_axes():
    # Y and X are not the last two here; taking scales[-2:] would give Z/C.
    assert crop_preview_scale(["Y", "X", "C"], [0.1, 0.2, 1.0]) == (0.1, 0.2)


def test_scale_falls_back_to_pixels_when_unusable():
    assert crop_preview_scale(["Y", "X"], []) == (1.0, 1.0)
    assert crop_preview_scale(["T", "Z"], [1.0, 1.0]) == (1.0, 1.0)
    assert crop_preview_scale(["Y", "X"], [0.0, 0.1]) == (1.0, 1.0)
    assert crop_preview_scale(["Y", "X"], [math.nan, 0.1]) == (1.0, 1.0)

"""Crop dialog X/Y rectangle-preview geometry (pure, Qt-free)."""
from imswitch.improcess.view.StackSubsetDialog import crop_preview_rectangle


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

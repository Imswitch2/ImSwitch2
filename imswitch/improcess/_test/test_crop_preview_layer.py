"""The crop preview is drawn in the displayed image's physical frame.

ImProcess shows a result with its ``axis_scales`` as the image layer's scale.
The preview rectangle is built in pixel indices, so a preview layer without
the same scale was ``1 / pixel size`` times too large on a calibrated image.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("napari")
from napari.components import ViewerModel  # noqa: E402
from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.view.StackSubsetDialog import (  # noqa: E402
    StackSubsetDialog,
    StackSubsetRangesWidget,
)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _result(scales, shape=(64, 128), labels=("Y", "X")):
    return SimpleNamespace(
        data=np.zeros(shape), axis_labels=list(labels), axis_scales=list(scales)
    )


def _viewer_showing(result):
    viewer = ViewerModel()
    viewer.add_image(result.data, scale=result.axis_scales)
    return viewer


def _preview(viewer):
    return next(layer for layer in viewer.layers if layer.name == "Crop preview")


@pytest.mark.parametrize("pixel_size", [0.1, 100.0])
def test_full_range_preview_spans_the_image_in_world_units(qapp, pixel_size):
    result = _result((pixel_size, pixel_size))
    viewer = _viewer_showing(result)
    dialog = StackSubsetDialog(result, napari_viewer=viewer)
    try:
        preview = _preview(viewer)
        assert tuple(preview.scale) == pytest.approx((pixel_size, pixel_size))
        # 64 x 128 pixels at pixel_size each -- not 64 x 128 world units.
        assert preview.extent.world[1] == pytest.approx(
            [64 * pixel_size, 128 * pixel_size]
        )
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_preview_scale_comes_from_the_y_and_x_axes_of_a_stack(qapp):
    result = _result((2.0, 0.1, 0.1), shape=(5, 64, 128), labels=("Z", "Y", "X"))
    viewer = _viewer_showing(result)
    dialog = StackSubsetDialog(result, napari_viewer=viewer)
    try:
        dialog.ranges._rows[2].firstSpin.setValue(11)   # X from pixel 11
        assert tuple(_preview(viewer).scale) == pytest.approx((0.1, 0.1))
        assert _preview(viewer).extent.world[0][1] == pytest.approx(1.0)
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_outline_is_two_screen_pixels_on_a_calibrated_image(qapp):
    result = _result((0.1, 0.1))
    viewer = _viewer_showing(result)
    viewer.camera.zoom = 50.0
    dialog = StackSubsetDialog(result, napari_viewer=viewer)
    try:
        preview = _preview(viewer)
        # napari draws edge_width x layer scale x zoom screen pixels.
        on_screen = preview.edge_width[0] * 0.1 * viewer.camera.zoom
        assert on_screen == pytest.approx(StackSubsetRangesWidget.PREVIEW_EDGE_PX)
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_retargeting_moves_the_preview_to_the_new_pixel_size(qapp):
    first = _result((0.1, 0.1))
    viewer = _viewer_showing(first)
    widget = StackSubsetRangesWidget(first, napari_viewer=viewer)
    try:
        preview = _preview(viewer)
        widget.setResult(_result((0.5, 0.5), shape=(32, 32)))
        assert _preview(viewer) is preview
        assert tuple(preview.scale) == pytest.approx((0.5, 0.5))
        assert preview.extent.world[1] == pytest.approx([16.0, 16.0])
    finally:
        widget._remove_crop_preview()
        widget.deleteLater()


def test_preview_is_removed_when_the_dialog_closes(qapp):
    result = _result((0.1, 0.1))
    viewer = _viewer_showing(result)
    dialog = StackSubsetDialog(result, napari_viewer=viewer)
    dialog.reject()
    dialog.deleteLater()
    assert "Crop preview" not in [layer.name for layer in viewer.layers]

"""Markers: a points (or shapes) layer from napari's own new-layer buttons is
fitted to the image it is drawn on, and the tools' points are a few screen
pixels across.

Before: with nothing selected the markers layer was at scale 1 in a µm world
(napari's default size 10 drew 10 µm points on a 33 µm field), and one made
while a z stack was selected was 3-D and kept the viewer 3-D after the scan
returned to 2-D.
"""
import numpy as np
import pytest
from napari._app_model.actions._file import add_new_points, add_new_shapes
from qtpy import QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol._test.unit._napari_model_viewer import (
    HeadlessImageWidget, activeName, layerNames,
)

pytestmark = pytest.mark.nohardware

PIXEL = 0.065


@pytest.fixture
def widget(qapp):
    widget = HeadlessImageWidget()
    widget.setLiveViewLayers(['Camera', 'APD'])
    widget.setImage('Camera', np.zeros((512, 512), np.uint16), (PIXEL, PIXEL))
    widget.setOverlayPixelScale((PIXEL, PIXEL), 'Camera')
    widget.napariViewer.reset_view()
    yield widget
    widget.close()


def _settle():
    for _ in range(3):
        QtWidgets.QApplication.processEvents()


def _screenPixels(viewer, layer, size):
    return size * float(layer.scale[-1]) * float(viewer.camera.zoom)


def test_new_points_with_nothing_selected_take_the_live_image_scale_and_a_screen_size(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.clear()
    add_new_points(viewer)
    _settle()
    markers = viewer.layers['Points']
    assert tuple(markers.scale) == pytest.approx((PIXEL, PIXEL))
    assert _screenPixels(viewer, markers, markers.current_size) == pytest.approx(
        naparitools.MARKER_SCREEN_PIXELS, rel=0.05)


def test_new_points_on_a_z_stack_are_two_dimensional(widget):
    viewer = widget.napariViewer
    widget.setImage('APD', np.zeros((6, 64, 64), np.uint16), (0.5, 0.1, 0.1))
    viewer.layers.selection.active = widget.imgLayers['APD']
    add_new_points(viewer)
    _settle()
    markers = viewer.layers['Points']
    assert markers.ndim == 2
    assert tuple(markers.scale) == pytest.approx((0.1, 0.1))
    assert activeName(viewer) == 'Points'
    assert layerNames(viewer).count('Points') == 1

    markers.add([3.0, 3.0])
    widget.setImage('APD', np.zeros((64, 64), np.uint16), (0.1, 0.1))   # back to 2-D
    assert viewer.dims.ndim == 2


def test_new_shapes_are_fitted_too(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.active = widget.imgLayers['Camera']
    add_new_shapes(viewer)
    _settle()
    shapes = viewer.layers['Shapes']
    assert shapes.ndim == 2 and tuple(shapes.scale) == pytest.approx((PIXEL, PIXEL))


def test_marker_points_keep_their_screen_size_on_zoom(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.clear()
    add_new_points(viewer)
    _settle()
    markers = viewer.layers['Points']
    markers.add([10.0, 10.0])
    viewer.camera.zoom = viewer.camera.zoom * 4
    assert _screenPixels(viewer, markers, markers.size[0]) == pytest.approx(
        naparitools.MARKER_SCREEN_PIXELS, rel=0.05)


def test_a_named_or_non_empty_points_layer_is_left_alone(widget):
    viewer = widget.napariViewer
    mine = viewer.add_points(name='my beads', ndim=3, size=10)
    filled = viewer.add_points(np.array([[1.0, 2.0]]), size=10)
    _settle()
    assert mine.ndim == 3 and mine.current_size == 10
    assert tuple(filled.scale) == (1.0, 1.0)


def test_the_tools_points_are_a_few_screen_pixels_across(widget):
    viewer = widget.napariViewer
    widget.toolManager.set_mode('point')
    layer = viewer.layers['Viewer Tool Points']
    assert tuple(layer.scale) == (1.0, 1.0)                     # world coordinates
    assert _screenPixels(viewer, layer, layer.current_size) == pytest.approx(
        naparitools.MARKER_SCREEN_PIXELS, rel=0.05)


def test_napari_s_button_works_with_a_two_d_layer_selected_beside_a_stack(widget):
    """napari 0.7's button passed the viewer's three units to a 2-D layer and
    raised; the viewer trims them to the layer's axes."""
    viewer = widget.napariViewer
    widget.setImage('APD', np.zeros((6, 64, 64), np.uint16), (0.5, 0.1, 0.1))
    viewer.layers.selection.active = widget.imgLayers['Camera']
    add_new_points(viewer)
    add_new_shapes(viewer)
    _settle()
    assert {'Points', 'Shapes'} <= set(layerNames(viewer))

"""A live-layer rebuild gives the layer selection back.

napari selects every layer it adds; a live layer is rebuilt when its number
of dimensions changes (a z-stack after a 2-D scan, 3D display, a Sum box), so
each rebuild took the selection from what the user was working on -- a
markers layer in add mode stopped receiving clicks. Gamma and opacity were
also lost on the rebuild.
"""
import numpy as np
import pytest

from imswitch.imcontrol._test.unit._napari_model_viewer import (
    HeadlessImageWidget, activeName,
)

pytestmark = pytest.mark.nohardware


@pytest.fixture
def widget(qapp):
    widget = HeadlessImageWidget()
    widget.setLiveViewLayers(['APD', 'Camera'])
    widget.setImage('APD', np.ones((32, 32), np.uint16), (0.1, 0.1))
    widget.setImage('Camera', np.ones((32, 32), np.uint16), (0.1, 0.1))
    yield widget
    widget.close()


def _stack(planes=4):
    return np.ones((planes, 32, 32), np.uint16)


def test_a_markers_layer_in_add_mode_stays_active_across_a_dimension_change(widget):
    viewer = widget.napariViewer
    markers = viewer.add_points(name='beads', ndim=2)
    markers.mode = 'add'

    widget.setImage('APD', _stack(), (0.5, 0.1, 0.1))            # rebuild

    assert activeName(viewer) == 'beads'
    assert str(markers.mode) == 'add'


def test_the_rebuilt_live_layer_stays_active_when_it_was(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.active = widget.imgLayers['APD']
    widget.setImage('APD', _stack(), (0.5, 0.1, 0.1))
    assert viewer.layers.selection.active is widget.imgLayers['APD']


def test_a_multi_selection_is_kept_and_followed_to_the_new_layer(widget):
    viewer = widget.napariViewer
    selection = viewer.layers.selection
    selection.clear()
    selection.update({widget.imgLayers['APD'], widget.imgLayers['Camera']})
    widget.setImage('APD', _stack(), (0.5, 0.1, 0.1))
    assert set(selection) == {widget.imgLayers['APD'], widget.imgLayers['Camera']}


def test_an_empty_selection_stays_empty(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.clear()
    widget.setImage('APD', _stack(), (0.5, 0.1, 0.1))
    assert len(viewer.layers.selection) == 0


def test_three_d_display_rebuild_keeps_the_selection(widget):
    viewer = widget.napariViewer
    shapes = viewer.add_shapes(name='notes', ndim=2)
    shapes.mode = 'add_rectangle'
    viewer.dims.ndisplay = 3
    assert activeName(viewer) == 'notes'
    viewer.dims.ndisplay = 2


def test_gamma_opacity_and_display_settings_survive_a_rebuild(widget):
    layer = widget.imgLayers['APD']
    layer.gamma = 0.6
    layer.opacity = 0.4
    layer.colormap = 'magma'
    layer.contrast_limits = (2, 9)
    layer.visible = False

    widget.setImage('APD', _stack(), (0.5, 0.1, 0.1))

    new = widget.imgLayers['APD']
    assert new is not layer
    assert new.gamma == pytest.approx(0.6)
    assert new.opacity == pytest.approx(0.4)
    assert new.colormap.name == 'magma'
    assert tuple(new.contrast_limits) == (2, 9)
    assert new.visible is False
    assert new.name == 'Live: APD'


def test_a_background_result_layer_does_not_take_the_selection(widget):
    viewer = widget.napariViewer
    viewer.layers.selection.active = widget.imgLayers['Camera']
    widget.setResultLayers('ImProcess: recon', [(np.zeros((8, 8)), {'name': 'Recon'}, 'image')])
    widget.upsertStaticLayer('Lifetime', np.zeros((8, 8)))
    assert activeName(viewer) == 'Live: Camera'

"""Viewer Tools on an existing tools layer: every drawing mode makes the
layer napari's active one (napari sends the mouse to the active layer only),
and a "Viewer Tools" layer the user deleted is replaced, not drawn into.

Before: switching rectangle -> line while another layer was selected left
the tools layer in ``add_line`` with the mouse going to the image; after a
delete, every tool drew into the orphaned layer and nothing appeared.
"""
import numpy as np
import pytest

from imswitch.imcontrol._test.unit._napari_model_viewer import (
    HeadlessImageWidget, activeName, layerNames,
)

pytestmark = pytest.mark.nohardware


@pytest.fixture
def widget(qapp):
    widget = HeadlessImageWidget()
    widget.setLiveViewLayers(['Camera'])
    widget.setImage('Camera', np.zeros((64, 64), np.uint16), (0.1, 0.1))
    yield widget
    widget.close()


def test_switching_rectangle_to_line_selects_the_existing_tools_layer(widget):
    viewer, tools = widget.napariViewer, widget.toolManager
    tools.set_mode('rectangle')
    layer = tools.get_layer()
    viewer.layers.selection.active = widget.imgLayers['Camera']   # user clicks the image layer

    tools.set_mode('line')

    assert activeName(viewer) == 'Viewer Tools'
    assert str(layer.mode) == 'add_line'


@pytest.mark.parametrize('mode', ['rectangle', 'line', 'ellipse', 'polygon', 'path', 'select'])
def test_every_drawing_mode_selects_the_tools_layer(widget, mode):
    viewer, tools = widget.napariViewer, widget.toolManager
    tools.set_mode('rectangle')
    viewer.layers.selection.active = widget.imgLayers['Camera']
    tools.set_mode(mode)
    assert activeName(viewer) == 'Viewer Tools'


def test_pan_leaves_the_selection_alone(widget):
    viewer, tools = widget.napariViewer, widget.toolManager
    tools.set_mode('rectangle')
    viewer.layers.selection.active = widget.imgLayers['Camera']
    tools.set_mode('pan')
    assert activeName(viewer) == 'Live: Camera'


def test_the_line_profile_path_selects_it_too(widget):
    viewer, service = widget.napariViewer, widget.toolService
    token = service.acquire('profile', 'rectangle')
    viewer.layers.selection.active = widget.imgLayers['Camera']
    service.set_mode(token, 'line')
    assert activeName(viewer) == 'Viewer Tools'


def test_a_deleted_tools_layer_is_replaced_not_drawn_into(widget):
    viewer, tools = widget.napariViewer, widget.toolManager
    tools.set_mode('rectangle')
    viewer.layers.remove(tools.get_layer())
    assert tools.get_shapes_data() == []

    tools.set_mode('line')

    layer = tools.get_layer()
    assert any(item is layer for item in viewer.layers)
    assert layerNames(viewer).count('Viewer Tools') == 1
    assert activeName(viewer) == 'Viewer Tools'
    layer.add_lines([[[0, 0], [5, 5]]])
    assert len(tools.get_shapes_data()) == 1


def test_a_deleted_points_layer_is_replaced(widget):
    viewer, tools = widget.napariViewer, widget.toolManager
    tools.set_mode('point')
    viewer.layers.remove(viewer.layers['Viewer Tool Points'])
    tools.set_mode('point')
    assert 'Viewer Tool Points' in layerNames(viewer)
    assert activeName(viewer) == 'Viewer Tool Points'

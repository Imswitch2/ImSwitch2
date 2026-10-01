"""The ROI manager offers every shape it can already capture.

The panel captured ellipses, polygons, lines and freehand strokes from the
start -- ``roi_from_shape`` has always mapped them -- but the only way in was
a button that said "Draw Rectangle". The other shapes were reachable only by
switching napari's own drawing mode, which is not somewhere a user would think
to look for a feature of this panel.
"""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_points import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.arange(256, dtype=float).reshape(16, 16)))
    yield widget
    widget.deleteLater()


def _choose(panel, mode):
    index = [m for _label, m in panel.DRAW_MODES].index(mode)
    panel.shapeCombo.setCurrentIndex(index)


#: Drawn shape -> the ROI type it must become. The point of the chooser is
#: that each of these is one selection away.
SHAPES = {
    "rectangle": "rectangle",
    "ellipse": "ellipse",
    "polygon": "polygon",
    "path": "freehand",
    "line": "line",
}


def test_every_shape_the_tool_supports_is_offered(panel):
    offered = {mode for _label, mode in panel.DRAW_MODES}
    assert offered == set(SHAPES) | {"point"}


def test_the_rectangle_is_still_the_default(panel):
    """Changing what the panel opens on would change what a habit draws."""
    assert panel.selected_draw_mode() == "rectangle"


@pytest.mark.parametrize("mode", sorted(SHAPES) + ["point"])
def test_choosing_a_shape_and_pressing_draw_sets_that_mode(panel, mode):
    _choose(panel, mode)
    panel._startDrawing()
    assert panel._toolService.get_mode() == mode


def test_switching_shape_while_drawing_takes_effect_at_once(panel):
    """The next drag should be the shape just picked, not the one before."""
    _choose(panel, "rectangle")
    panel._startDrawing()

    _choose(panel, "ellipse")

    assert panel._toolService.get_mode() == "ellipse"


def test_switching_shape_before_drawing_does_not_grab_the_tool(panel):
    """Opening the chooser must not take the tool from another panel: the
    panel registers at construction but only becomes the active owner when
    Draw is pressed."""
    _choose(panel, "ellipse")
    assert not panel._toolService.is_active(panel.TOOL_OWNER)

    panel._startDrawing()
    assert panel._toolService.is_active(panel.TOOL_OWNER)


@pytest.mark.parametrize("mode,roi_type", sorted(SHAPES.items()))
def test_each_drawn_shape_is_added_as_its_own_kind(panel, mode, roi_type):
    _choose(panel, mode)
    panel._startDrawing()
    layer = panel._toolService.manager.get_layer()
    layer.add_shape([[2.0, 2.0], [2.0, 8.0], [8.0, 8.0], [8.0, 2.0]], mode)

    panel.add_current_rectangle()

    assert [roi.roi_type for roi in panel.rois()] == [roi_type]


def test_shapes_of_different_kinds_can_be_captured_together(panel):
    """The capture takes the layer, not the chooser, so a rectangle drawn
    before switching to an ellipse is not left behind."""
    _choose(panel, "rectangle")
    panel._startDrawing()
    layer = panel._toolService.manager.get_layer()
    layer.add_shape([[1.0, 1.0], [1.0, 5.0], [5.0, 5.0], [5.0, 1.0]], "rectangle")
    _choose(panel, "ellipse")
    layer.add_shape([[6.0, 6.0], [6.0, 12.0], [12.0, 12.0], [12.0, 6.0]], "ellipse")

    panel.add_current_rectangle()

    assert sorted(roi.roi_type for roi in panel.rois()) == ["ellipse", "rectangle"]


def test_a_drawn_ellipse_measures_as_an_ellipse(panel):
    """The chooser is only worth having if what it draws measures correctly --
    an ellipse added this way must not come out as its bounding box."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local

    _choose(panel, "ellipse")
    panel._startDrawing()
    layer = panel._toolService.manager.get_layer()
    layer.add_shape([[0.0, 0.0], [0.0, 12.0], [12.0, 12.0], [12.0, 0.0]], "ellipse")
    panel.add_current_rectangle()

    mask, _slices = roi_mask_local(panel.rois()[0], (16, 16))
    assert mask.sum() == pytest.approx(np.pi * 6 * 6, rel=0.1)
    assert mask.sum() < mask.size

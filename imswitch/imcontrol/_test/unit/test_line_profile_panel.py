"""imcontrol's Line Profile dock and its Viewer Tools buttons.

The dock is ImProcess's Profile widget (one class, in imcommon), and Viewer
Tools' Line / Rectangle ROI / ROI Intensity vs T buttons select its modes, so
there is one drawing tool with two sets of buttons, not two tools on one layer.
"""

import ast
import inspect
from types import SimpleNamespace

import numpy as np
import pytest
from napari.components import ViewerModel
from qtpy import QtWidgets

from imswitch.imcommon.view.guitools.ProfileWidget import ProfileWidget
from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService
from imswitch.imcontrol.controller.controllers.ViewerToolsController import (
    ViewerToolsController,
)
from imswitch.imcontrol.view.widgets import LineProfileWidget as line_profile_module
from imswitch.imcontrol.view.widgets.LineProfileWidget import LineProfileWidget
from imswitch.imcontrol.view.widgets.ViewerToolsWidget import ViewerToolsWidget

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _viewer():
    viewer = ViewerModel()
    viewer.add_image(np.ones((16, 16)), name="Live: cam", scale=(0.2, 0.2))
    return viewer


def _controller(viewer, lineProfile):
    tools = ViewerToolsWidget(None)
    image = SimpleNamespace(
        napariViewer=viewer,
        toolManager=ViewerToolService.for_viewer(viewer).manager,
    )
    controller = ViewerToolsController(
        None, None, None,
        widget=tools, factory=None, moduleCommChannel=None,
        imageWidget=image, lineProfileWidget=lineProfile,
    )
    return controller, tools


def test_the_dock_is_the_shared_profile_widget_set_up_for_live_images(qapp):
    panel = LineProfileWidget(None, napariViewer=_viewer())
    profile = panel.profile

    assert isinstance(profile, ProfileWidget)
    assert "timetrace" in profile.modes()
    assert profile.timeTraceButton is not None
    assert not profile.liveCheck.isHidden()
    # No Results table or Graph panel in imcontrol to push to.
    assert profile.pushButton.isHidden()
    assert profile.pushGraphButton.isHidden()
    # Live layers are scaled by the detector pixel size in micrometres.
    assert profile._distanceUnit() == "µm"


def test_the_dock_explains_itself_without_an_image_display(qapp):
    panel = LineProfileWidget(None)
    assert panel.profile is None
    labels = panel.findChildren(QtWidgets.QLabel)
    assert any("image display" in label.text() for label in labels)


def test_the_dock_has_no_sampler_of_its_own():
    """It had one (map_coordinates) that drifted from ImProcess's."""
    tree = ast.parse(inspect.getsource(line_profile_module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "map_coordinates" not in imported


def test_viewer_tools_buttons_select_the_profile_mode(qapp):
    viewer = _viewer()
    panel = LineProfileWidget(None, napariViewer=viewer)
    _controller_, tools = _controller(viewer, panel)

    assert not tools.intensityTraceButton.isHidden()

    tools.lineButton.click()
    assert panel.profile.mode() == "line"

    tools.intensityTraceButton.click()
    assert panel.profile.mode() == "timetrace"
    assert panel.profile._traceTimer.isActive()

    tools.rectangleButton.click()
    assert panel.profile.mode() == "rectangle"
    assert not panel.profile._traceTimer.isActive()


def test_the_profile_panels_own_buttons_are_mirrored_in_viewer_tools(qapp):
    viewer = _viewer()
    panel = LineProfileWidget(None, napariViewer=viewer)
    _controller_, tools = _controller(viewer, panel)

    panel.profile.selectMode("timetrace")
    assert tools.getActiveTool() == "timetrace"

    # Z profile draws a rectangle and has no button of its own there.
    panel.profile.selectMode("zprofile")
    assert tools.getActiveTool() == "rectangle"


def test_crosshair_stops_the_profile_drawing_and_is_disarmed_by_it(qapp):
    viewer = _viewer()
    panel = LineProfileWidget(None, napariViewer=viewer)
    controller, tools = _controller(viewer, panel)
    tools.lineButton.click()

    tools.crosshairButton.click()
    # The profile went back to pan, without dragging Viewer Tools with it.
    assert panel.profile.mode() == "pan"
    assert tools.getActiveTool() == "crosshair"
    assert controller._crosshair_click_cb in viewer.mouse_drag_callbacks

    # Picking a profile mode on the panel must not leave a crosshair that
    # re-places itself — wiping the new line — on every click.
    panel.profile.selectMode("line")
    assert controller._crosshair_click_cb is None
    assert tools.getActiveTool() == "line"


def test_without_a_line_profile_viewer_tools_draw_on_their_own(qapp):
    viewer = _viewer()
    controller, tools = _controller(viewer, None)

    assert tools.intensityTraceButton.isHidden()
    tools.lineButton.click()
    assert controller._toolManager.get_mode() == "line"

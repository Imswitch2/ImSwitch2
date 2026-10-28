"""The window drawing tool and the active-window viewer facade.

The token tests mirror ``test_viewer_tool_service.py`` (same questions, same
answers) so the two services are held to one contract; the vertex-format tests
compare against the napari format written out by hand.
"""

from __future__ import annotations

import numpy as np
import pytest
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.algorithms.layer_selection import active_image_layer
from imswitch.imcommon.view.guitools.viewer_tools import StaleToolToken, ViewerToolService
from imswitch.improcess.analysis.roi_frame_adapter import frame_from_layer, plane_position, world_to_data
from imswitch.improcess.analysis.roi_manager import roi_from_shape
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import ViewMode
from imswitch.improcess.view.image_window import PermutedArray
from imswitch.improcess.view.result_windows import ResultWindowManager
from imswitch.improcess.view.window_tools import (
    ActiveWindowViewer,
    UnsupportedToolMode,
    WindowToolService,
)


def _rect(r0, c0, r1, c1):
    """The napari rectangle, as ``test_viewer_tool_service`` writes it."""
    return [[r0, c0], [r0, c1], [r1, c1], [r1, c0]]


def _result(name, data, labels, **kwargs):
    return ArrayProcessingResult(name, data, list(labels), **kwargs)


@pytest.fixture
def rig(qtbot):
    """A window manager with one 3-D result shown, and the facade bound to it."""
    manager = ResultWindowManager()
    viewer = ActiveWindowViewer()
    viewer.bind(manager)
    data = np.arange(3 * 40 * 50, dtype=np.float32).reshape(3, 40, 50)
    window = manager.show(_result("stack", data, "ZYX"))
    qtbot.addWidget(window)
    yield SimpleNamespaceRig(manager, viewer, window, data)
    manager.closeAll()


class SimpleNamespaceRig:
    def __init__(self, manager, viewer, window, data):
        self.manager, self.viewer, self.window, self.data = manager, viewer, window, data
        self.service = viewer.tool_service


# --------------------------------------------------------------------------
# the service is a ViewerToolService, found the way the panels find it
# --------------------------------------------------------------------------

def test_the_panels_get_the_window_service_for_the_facade(rig):
    assert isinstance(rig.service, ViewerToolService)
    assert ViewerToolService.for_viewer(rig.viewer) is rig.service


# --------------------------------------------------------------------------
# token semantics (mirrors test_viewer_tool_service.py)
# --------------------------------------------------------------------------

def test_registering_does_not_take_the_tool(rig):
    token = rig.service.register("roi-stats")
    assert not rig.service.is_active("roi-stats")
    with pytest.raises(StaleToolToken):
        rig.service.set_mode(token, "rectangle")


def test_clearing_one_owner_keeps_another_owners_shapes(rig):
    service = rig.service
    stats = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))
    profile = service.acquire("profile", "line")
    service.add_shape("line", [[0, 0], [5, 5]])

    service.clear(profile)

    assert [s[1] for s in service.shapes(stats)] == ["rectangle"]
    assert service.shapes(profile) == []


def test_owner_only_sees_its_own_shapes(rig):
    service = rig.service
    stats = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))
    profile = service.acquire("profile", "rectangle")
    service.add_shape("rectangle", _rect(6, 6, 9, 9))

    assert len(service.shapes(stats)) == 1 and len(service.shapes(profile)) == 1
    assert service.shapes(stats)[0][0] != service.shapes(profile)[0][0]


def test_single_shape_limit_is_per_owner_not_global(rig):
    service = rig.service
    stats = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))
    profile = service.acquire("profile", "rectangle")
    service.add_shape("rectangle", _rect(6, 6, 9, 9))
    service.add_shape("rectangle", _rect(10, 10, 12, 12))

    assert len(service.shapes(profile)) == 1
    assert len(service.shapes(stats)) == 1
    assert len(rig.service._shapes_of(rig.window)) == 2
    # The newest rectangle is the one kept.
    assert service.shapes(profile)[0][2][:, 0].min() == 10


def test_a_multi_shape_owner_keeps_every_rectangle(rig):
    service = rig.service
    manager = service.acquire("roi-manager")
    service.set_multi_shape(manager, True)
    for k in range(3):
        service.add_shape("rectangle", _rect(k, k, k + 5, k + 5))
    assert len(service.shapes(manager)) == 3


def test_stale_token_raises(rig):
    old = rig.service.acquire("roi-stats", "rectangle")
    rig.service.acquire("roi-stats", "rectangle")
    with pytest.raises(StaleToolToken):
        rig.service.clear(old)


def test_closed_and_reopened_panel_reclaims_its_shapes(rig):
    service = rig.service
    first = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))

    service.release(first)
    assert len(service._shapes_of(rig.window)) == 1, "release discarded the shapes"

    reopened = service.register("roi-stats")
    assert len(service.shapes(reopened)) == 1


def test_release_can_discard_and_is_idempotent(rig):
    service = rig.service
    token = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))
    service.release(token, discard_shapes=True)
    service.release(token)  # a stale token is a no-op
    assert service._shapes_of(rig.window) == []


def test_release_discarding_drops_only_that_owners_shapes(rig):
    service = rig.service
    stats = service.acquire("roi-stats", "rectangle")
    service.add_shape("rectangle", _rect(0, 0, 4, 4))
    profile = service.acquire("profile", "line")
    service.add_shape("line", [[0, 0], [5, 5]])

    service.release(profile, discard_shapes=True)

    assert [s.shape_type for s in service._shapes_of(rig.window)] == ["rectangle"]
    assert len(service.shapes(stats)) == 1


def test_preemption_notifies_but_does_not_delete(rig):
    preempted = []
    rig.service.sigToolPreempted.connect(preempted.append)
    rig.service.acquire("roi-stats", "rectangle")
    rig.service.add_shape("rectangle", _rect(0, 0, 4, 4))
    rig.service.acquire("profile", "line")
    assert preempted == ["roi-stats"]
    assert len(rig.service._shapes_of(rig.window)) == 1


def test_handlers_fire_for_the_active_owner_only_and_stop_after_release(rig):
    service = rig.service
    stats_calls, profile_calls = [], []
    stats = service.acquire("roi-stats", "rectangle")
    service.on_shapes_changed(stats, lambda: stats_calls.append(1))
    profile = service.acquire("profile", "line")
    service.on_shapes_changed(profile, lambda: profile_calls.append(1))

    service.add_shape("line", [[0, 0], [5, 5]])
    assert profile_calls == [1] and stats_calls == []

    service.release(profile)
    service.add_shape("line", [[0, 0], [6, 6]])
    assert profile_calls == [1]


def test_viewer_events_registered_through_the_service_are_torn_down(rig):
    calls = []
    token = rig.service.acquire("roi-stats")
    rig.service.on_viewer_event(token, rig.viewer.dims.events.current_step,
                                lambda _e=None: calls.append(1))
    rig.window.moveDimStep(0, 1)
    assert calls
    calls.clear()
    rig.service.release(token)
    rig.window.moveDimStep(0, 2)
    assert calls == []


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["rectangle", "ellipse", "polygon", "line", "point"])
def test_draw_modes_take_the_canvas(rig, mode):
    token = rig.service.acquire("roi-manager", mode)
    assert rig.service.get_mode() == mode
    assert rig.window.viewBox().drawHandler is not None
    rig.service.set_mode(token, "pan")
    assert rig.window.viewBox().drawHandler is None


@pytest.mark.parametrize("mode", ["path", "select", "lasso"])
def test_other_modes_are_refused_with_a_clear_message(rig, mode):
    token = rig.service.acquire("roi-manager")
    with pytest.raises(UnsupportedToolMode, match="not supported with separate windows"):
        rig.service.set_mode(token, mode)


# --------------------------------------------------------------------------
# the vertex format
# --------------------------------------------------------------------------

def test_a_rectangle_has_the_napari_vertices_and_makes_the_same_roi(rig):
    service = rig.service
    token = service.acquire("roi-manager", "rectangle")
    service.add_shape("rectangle", _rect(5.0, 7.0, 20.0, 30.0))

    [(index, shape_type, vertices)] = service.shapes(token)
    napari = np.asarray(_rect(5.0, 7.0, 20.0, 30.0), dtype=np.float64)
    assert shape_type == "rectangle"
    assert vertices.dtype == np.float64 and vertices.shape == (4, 2)
    assert np.allclose(vertices, napari)
    # What the panels read back from the same drawing.
    assert np.allclose(service.get_rectangle_bounds(index), (5.0, 7.0, 20.0, 30.0))
    assert roi_from_shape(vertices, shape_type=shape_type, name="r") == \
        roi_from_shape(napari, shape_type="rectangle", name="r")


def test_the_mouse_draws_the_same_vertices_as_the_programmatic_path(rig):
    service = rig.service
    token = service.acquire("roi-manager", "rectangle")
    handler = rig.window.viewBox().drawHandler
    assert handler("drag-start", (7.0, 5.0), None)       # (x=col, y=row)
    assert handler("drag", (20.0, 12.0), None)
    assert rig.service._state(rig.window).preview is not None   # a dashed preview while dragging
    assert handler("drag-finish", (30.0, 20.0), None)

    [(_, _, vertices)] = service.shapes(token)
    assert np.allclose(vertices, _rect(5.0, 7.0, 20.0, 30.0))


def _show(window):
    window.resize(500, 500)
    window.show()
    QtWidgets.QApplication.processEvents()


def _mouse_drag(window):
    """A left-button drag across the canvas, as Qt delivers it."""
    viewport = window._canvas.viewport()

    def send(kind, x, y, button, buttons):
        event = QtGui.QMouseEvent(kind, QtCore.QPointF(x, y), button, buttons, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(viewport, event)
        QtWidgets.QApplication.processEvents()

    send(QtCore.QEvent.MouseButtonPress, 100, 100, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
    for k in range(1, 11):
        send(QtCore.QEvent.MouseMove, 100 + 20 * k, 100 + 10 * k, QtCore.Qt.NoButton,
             QtCore.Qt.LeftButton)
    send(QtCore.QEvent.MouseButtonRelease, 300, 200, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)


def test_a_real_mouse_drag_on_the_canvas_draws_a_rectangle(qtbot, rig):
    """The whole path: Qt mouse events -> pyqtgraph -> the view box -> the tool."""
    token = rig.service.acquire("roi-manager", "rectangle")
    _show(rig.window)
    before = rig.window.viewBox().viewRect()
    _mouse_drag(rig.window)

    [(_, shape_type, vertices)] = rig.service.shapes(token)
    assert shape_type == "rectangle"
    assert rig.window.viewBox().viewRect() == before       # drawing does not pan
    # 200 px right and 100 px down: twice as wide as tall.
    assert (vertices[:, 1].max() - vertices[:, 1].min()) == pytest.approx(
        2 * (vertices[:, 0].max() - vertices[:, 0].min()), rel=0.05)


def test_a_real_mouse_drag_in_pan_mode_pans_and_draws_nothing(qtbot, rig):
    token = rig.service.acquire("roi-manager", "pan")
    _show(rig.window)
    before = rig.window.viewBox().viewRect()
    _mouse_drag(rig.window)
    assert rig.service.shapes(token) == []
    assert rig.window.viewBox().viewRect() != before


def test_a_tiny_drag_draws_nothing(rig):
    service = rig.service
    token = service.acquire("roi-manager", "rectangle")
    handler = rig.window.viewBox().drawHandler
    handler("drag-start", (7.0, 5.0), None)
    handler("drag-finish", (7.0, 5.0), None)
    assert service.shapes(token) == []


def test_an_ellipse_is_its_bounding_box_corners(rig):
    service = rig.service
    token = service.acquire("roi-manager", "ellipse")
    handler = rig.window.viewBox().drawHandler
    handler("drag-start", (10.0, 8.0), None)
    handler("drag-finish", (30.0, 28.0), None)

    [(_, shape_type, vertices)] = service.shapes(token)
    assert shape_type == "ellipse"
    assert np.allclose(vertices, _rect(8.0, 10.0, 28.0, 30.0))
    roi = roi_from_shape(vertices, shape_type=shape_type, name="e")
    assert roi.roi_type == "ellipse" and len(roi.vertices) == 4


def test_a_line_has_its_two_end_points(rig):
    service = rig.service
    token = service.acquire("profile", "line")
    handler = rig.window.viewBox().drawHandler
    handler("drag-start", (3.0, 4.0), None)       # x=3 (col), y=4 (row)
    handler("drag-finish", (25.0, 30.0), None)

    [(index, shape_type, vertices)] = service.shapes(token)
    assert shape_type == "line"
    assert np.allclose(vertices, [[4.0, 3.0], [30.0, 25.0]])
    assert np.allclose(service.get_line_endpoints(index), ((4.0, 3.0), (30.0, 25.0)))


def test_a_polygon_is_clicked_out_and_closed_by_a_double_click(rig):
    service = rig.service
    token = service.acquire("roi-manager", "polygon")
    handler = rig.window.viewBox().drawHandler
    for xy in ((5.0, 5.0), (25.0, 6.0), (15.0, 22.0)):
        assert handler("click", xy, None)
    assert service.shapes(token) == []                  # not closed yet
    assert handler("click", (15.0, 22.0), None)         # the click before a double click
    assert handler("double-click", (15.0, 22.0), None)

    [(_, shape_type, vertices)] = service.shapes(token)
    assert shape_type == "polygon"
    assert np.allclose(vertices, [[5.0, 5.0], [6.0, 25.0], [22.0, 15.0]])
    roi = roi_from_shape(vertices, shape_type=shape_type, name="p")
    assert roi.roi_type == "polygon" and len(roi.vertices) == 3


def test_a_polygon_of_two_vertices_is_not_a_shape(rig):
    token = rig.service.acquire("roi-manager", "polygon")
    handler = rig.window.viewBox().drawHandler
    handler("click", (5.0, 5.0), None)
    handler("click", (25.0, 6.0), None)
    handler("double-click", (25.0, 6.0), None)
    assert rig.service.shapes(token) == []


def test_points_are_clicks_in_row_col_order_and_clear_with_the_tool(rig):
    service = rig.service
    token = service.acquire("roi-manager", "point")
    handler = rig.window.viewBox().drawHandler
    fired = []
    service.on_shapes_changed(token, lambda: fired.append(1))
    handler("click", (7.0, 5.0), None)
    handler("click", (30.0, 20.0), None)
    assert service.points(token) == [(5.0, 7.0), (20.0, 30.0)]
    assert fired == [1, 1]
    service.clear(token)
    assert service.points(token) == []


def test_editing_a_shape_notifies_like_napari_does(rig):
    service = rig.service
    token = service.acquire("roi-stats", "rectangle")
    fired = []
    service.on_shapes_changed(token, lambda: fired.append(1))
    shape = service.add_shape("rectangle", _rect(5, 5, 15, 15))
    assert fired == [1]
    shape.item.setPos(QtCore.QPointF(10, 10))   # moved by the user: pyqtgraph announces it
    assert fired == [1, 1]
    [(_, _, vertices)] = service.shapes(token)
    assert np.allclose(vertices, _rect(10, 10, 20, 20))


def test_an_edited_ellipse_reports_its_new_box(rig):
    service = rig.service
    token = service.acquire("roi-manager", "ellipse")
    shape = service.add_shape("ellipse", _rect(5, 5, 15, 25))
    shape.item.setSize((40.0, 10.0))
    [(_, _, vertices)] = service.shapes(token)
    assert np.allclose(vertices, _rect(5, 5, 15, 45))


# --------------------------------------------------------------------------
# following the active window
# --------------------------------------------------------------------------

def test_shapes_belong_to_their_window_and_the_service_follows_the_active_one(rig):
    service, manager, first = rig.service, rig.manager, rig.window
    token = service.acquire("roi-manager", "rectangle")
    service.set_multi_shape(token, True)
    service.add_shape("rectangle", _rect(2, 2, 10, 10))
    seen = []
    service.sigTargetLayerChanged.connect(seen.append)

    second = manager.show(_result("other", np.zeros((30, 30), np.float32), "YX"))
    assert manager.activeWindow() is second
    assert service.shapes(token) == []                    # the first window's shape is not here
    assert seen and seen[-1] is rig.viewer.layerFor(second)
    assert service.target_image_layer is rig.viewer.layerFor(second)
    # The draw tool follows: the new window's canvas is live.
    assert second.viewBox().drawHandler is not None
    service.add_shape("rectangle", _rect(3, 3, 8, 8))
    assert len(service.shapes(token)) == 1

    manager.show(first.result)
    assert manager.activeWindow() is first
    assert seen[-1] is rig.viewer.layerFor(first)
    [(_, _, vertices)] = service.shapes(token)
    assert np.allclose(vertices, _rect(2, 2, 10, 10))
    assert len(service._shapes_of(second)) == 1           # and the other is untouched


# --------------------------------------------------------------------------
# the viewer facade
# --------------------------------------------------------------------------

def test_dims_follow_the_active_windows_sliders_and_announce_them(rig):
    viewer, window = rig.viewer, rig.window
    steps = []
    viewer.dims.events.current_step.connect(steps.append)
    assert viewer.dims.current_step == (0, 0, 0)
    window.moveDimStep(0, 2)
    assert viewer.dims.current_step == (2, 0, 0)
    assert len(steps) == 1
    assert viewer.dims.axis_labels == ("Z", "Y", "X")


def test_switching_window_announces_layers_and_step(rig):
    viewer, manager = rig.viewer, rig.manager
    inserted, removed, active, steps = [], [], [], []
    viewer.layers.events.inserted.connect(inserted.append)
    viewer.layers.events.removed.connect(removed.append)
    viewer.layers.selection.events.active.connect(active.append)
    viewer.dims.events.current_step.connect(steps.append)
    first_layer = viewer.layerFor(rig.window)

    second = manager.show(_result("other", np.zeros((30, 30), np.float32), "YX"))

    second_layer = viewer.layerFor(second)
    assert removed == [first_layer] and inserted == [second_layer] and active == [second_layer]
    assert steps
    assert viewer.layers.selection.active is second_layer
    assert list(viewer.layers) == [second_layer]
    assert viewer.dims.current_step == (0, 0)


def test_without_a_window_there_is_no_layer(qtbot):
    viewer = ActiveWindowViewer()
    assert viewer.layers.selection.active is None
    assert list(viewer.layers) == []
    assert viewer.dims.current_step == ()
    assert viewer.tool_service.target_image_layer is None
    assert active_image_layer(viewer) is None


def test_the_helpers_read_the_facade_layer_like_a_napari_one(rig):
    viewer, window, data = rig.viewer, rig.window, rig.data
    layer = active_image_layer(viewer)
    assert layer is viewer.layerFor(window)

    # The full nD result, not the plane on screen.
    assert layer.data is data and layer.ndim == 3 and layer.name == "stack"
    assert layer.metadata["axis_labels"] == ["Z", "Y", "X"]
    assert layer.scale == (1.0, 1.0, 1.0)

    frame = frame_from_layer(layer, viewer)
    assert frame is not None
    assert [axis.label for axis in frame.axes] == ["Z", "Y", "X"]
    assert frame.shape == (40, 50)
    window.moveDimStep(0, 2)
    assert plane_position(viewer, frame) == (("Z", 2),)


def test_world_and_data_coordinates_use_the_windows_calibration(qtbot):
    manager = ResultWindowManager()
    viewer = ActiveWindowViewer(manager)
    window = manager.show(_result("cal", np.zeros((20, 30), np.float32), "YX",
                                  axis_scales=[0.5, 0.25], scale_unit="um"))
    qtbot.addWidget(window)
    layer = viewer.activeLayer()
    assert np.allclose(world_to_data(layer, (5.0, 2.0)), (10.0, 8.0))
    assert np.allclose(layer.data_to_world(np.array([10.0, 8.0])), (5.0, 2.0))
    # Drawn in calibrated coordinates, a shape comes out in them (the panels map it back).
    token = viewer.tool_service.acquire("roi-manager")
    viewer.tool_service.add_shape("rectangle", _rect(1.0, 1.0, 4.0, 3.0))
    [(_, _, vertices)] = viewer.tool_service.shapes(token)
    assert np.allclose(vertices, _rect(1.0, 1.0, 4.0, 3.0))
    manager.closeAll()


def test_an_rgb_window_is_not_a_measurable_layer(qtbot):
    from imswitch.improcess.model.result import DisplayLayerSpec

    class _Rgb:
        name = "rgb"
        data = np.zeros((10, 10, 3), np.uint8)
        axis_labels = ["Y", "X", "C"]
        axis_scales = [1.0, 1.0, 1.0]
        scale_unit = "px"

        def display_layers(self):
            return [DisplayLayerSpec("rgb", self.data, ["Y", "X", "C"], rgb=True)]

    manager = ResultWindowManager()
    viewer = ActiveWindowViewer(manager)
    window = manager.show(_Rgb())
    qtbot.addWidget(window)
    assert viewer.activeLayer() is None and active_image_layer(viewer) is None
    manager.closeAll()


def test_a_view_mode_with_reordered_axes_is_presented_in_displayed_order(qtbot):
    manager = ResultWindowManager()
    viewer = ActiveWindowViewer(manager)
    data = np.arange(4 * 6 * 8, dtype=np.float32).reshape(4, 6, 8)
    modes = [ViewMode("XY", (0, 1, 2)), ViewMode("XZ", (1, 0, 2))]
    window = manager.show(_result("m", data, "ZYX", view_modes=modes))
    qtbot.addWidget(window)
    window._modeCombo.setCurrentIndex(1)
    layer = viewer.activeLayer()
    assert layer.data.shape == (6, 4, 8) and layer.metadata["axis_labels"] == ["Y", "Z", "X"]
    assert np.array_equal(np.asarray(layer.data), data.transpose(1, 0, 2))
    assert np.array_equal(layer.data[2], data[:, 2, :])
    assert np.array_equal(layer.data[2, 1], data[1, 2, :])
    assert np.array_equal(layer.data[..., 3], data[:, :, 3].T)
    manager.closeAll()


def test_permuted_array_matches_numpy_for_every_kind_of_index():
    data = np.random.default_rng(0).random((3, 4, 5, 6))
    perm = (2, 0, 3, 1)
    view = PermutedArray(data, perm)
    expected = data.transpose(perm)
    assert view.shape == expected.shape and view.ndim == 4
    for key in (0, (1, 2), (slice(None), 1), (slice(None), slice(None), 2),
                (Ellipsis, 3), (1, slice(None), slice(None), 2), (slice(1, 3),)):
        assert np.array_equal(np.asarray(view[key]), expected[key]), key
    assert np.array_equal(np.asarray(view), expected)

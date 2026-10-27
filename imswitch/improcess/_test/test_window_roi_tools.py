"""The ROI Manager, ROI statistics and Profile on the separate image windows.

Real widgets, real :class:`ImageWindow`s and the real window drawing tool; only
the napari viewer is absent (it cannot run offscreen). Shapes are drawn through
the service the way the mouse would, and every number is checked against numpy
on the same plane.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pyqtgraph.dockarea import Dock
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.view import window_mode
from imswitch.improcess.view.ProfileWidget import ProfileWidget
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget
from imswitch.improcess.view.ROIStatsWidget import ROIStatsWidget
from imswitch.improcess.view.result_windows import ResultWindowManager
from imswitch.improcess.view.window_tools import ActiveWindowViewer

PLANES, ROWS, COLS = 4, 64, 80


def _rect(r0, c0, r1, c1):
    return [[r0, c0], [r0, c1], [r1, c1], [r1, c0]]


@pytest.fixture
def rig(qtbot):
    manager = ResultWindowManager()
    viewer = ActiveWindowViewer(manager)
    data = np.random.default_rng(7).random((PLANES, ROWS, COLS)).astype(np.float32)
    window = manager.show(ArrayProcessingResult("stack", data, ["Z", "Y", "X"]))
    qtbot.addWidget(window)

    class Rig:
        pass

    rig = Rig()
    rig.manager, rig.viewer, rig.window, rig.data = manager, viewer, window, data
    rig.service = viewer.tool_service
    yield rig
    manager.closeAll()


@pytest.fixture
def panel(qtbot, rig):
    widget = ROIManagerWidget(rig.viewer)
    qtbot.addWidget(widget)
    return widget


def _draw_and_add(panel, rig, shape_type, vertices):
    panel.shapeCombo.setCurrentIndex([m for _l, m in panel.DRAW_MODES].index(shape_type))
    panel._startDrawing()
    rig.service.add_shape(shape_type, vertices)
    panel.add_current_rectangle()


def _on(window, item) -> bool:
    """Whether ``item`` is drawn on ``window``'s canvas."""
    return item.getViewBox() is window.viewBox()


def _row(panel, index=0):
    return panel._stats_rows[index]


# --------------------------------------------------------------------------
# ROI Manager
# --------------------------------------------------------------------------

def test_a_drawn_rectangle_is_added_listed_and_measured_on_the_current_plane(panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))

    assert [roi.roi_type for roi in panel.rois()] == ["rectangle"]
    assert panel.rois()[0].bounds == (10, 30, 12, 40)
    assert panel.table.rowCount() == 1
    region = rig.data[0, 10:30, 12:40]
    row = _row(panel)
    assert row["area_px"] == region.size
    assert row["mean"] == pytest.approx(float(region.mean()), rel=1e-5)
    assert row["min"] == pytest.approx(float(region.min()))
    assert row["max"] == pytest.approx(float(region.max()))
    assert row["sum"] == pytest.approx(float(region.sum()), rel=1e-5)
    # The scratch shape was consumed by the capture.
    assert rig.service.shapes(panel._toolToken) == []


def test_moving_z_remeasures_the_same_roi_on_the_new_plane(qtbot, panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))
    first = _row(panel)["mean"]

    rig.window.moveDimStep(0, 2)
    qtbot.waitUntil(lambda: _row(panel)["mean"] != first, timeout=2000)

    assert _row(panel)["mean"] == pytest.approx(float(rig.data[2, 10:30, 12:40].mean()), rel=1e-5)


def test_the_roi_is_drawn_on_the_window_and_selecting_it_highlights_the_outline(panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))

    items = panel._overlay._items
    assert len(items) == 1
    x, y = items[0].getData()
    finite = np.isfinite(x)
    assert (np.nanmin(x), np.nanmax(x)) == (12.0, 40.0)         # columns
    assert (np.nanmin(y), np.nanmax(y)) == (10.0, 30.0)         # rows
    assert finite.sum() == 5                                    # a closed rectangle
    assert _on(rig.window, items[0])
    unselected = items[0].opts["pen"].color().name()
    assert unselected != "#ffffff"

    panel.table.selectRow(0)
    assert panel._overlay._items[0].opts["pen"].color().name() == "#ffffff"

    panel.showAllCheck.setChecked(False)
    assert panel._overlay._items == []


def test_clicking_an_outline_selects_its_row(panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))
    _draw_and_add(panel, rig, "rectangle", _rect(40, 50, 60, 70))
    assert panel.table.selectedItems() == []

    # A click on the first outline's edge (x=col, y=row), as the view box reports it.
    rig.window.viewBox().sigViewClicked.emit(12.0, 20.0, QtCore.Qt.NoModifier)

    assert {item.row() for item in panel.table.selectedItems()} == {0}
    # A click in empty space picks nothing new.
    panel.table.clearSelection()
    rig.window.viewBox().sigViewClicked.emit(75.0, 5.0, QtCore.Qt.NoModifier)
    assert panel.table.selectedItems() == []


def test_select_rename_and_delete(monkeypatch, panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))
    _draw_and_add(panel, rig, "rectangle", _rect(40, 50, 60, 70))
    assert panel.table.rowCount() == 2

    panel.table.selectRow(0)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("nucleus", True)))
    panel.rename_selected()
    assert [roi.name for roi in panel.rois()][0] == "nucleus"

    panel.table.selectRow(0)
    panel.delete_selected()
    assert [roi.bounds for roi in panel.rois()] == [(40, 60, 50, 70)]
    assert len(panel._overlay._items) == 1


def test_measure_pushes_the_roi_to_the_results_table(panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))
    pushed = []
    panel.sigResultPushed.connect(lambda columns, rows: pushed.append(rows))
    rig.window.moveDimStep(0, 1)

    panel.measure()

    [rows] = pushed
    assert rows[0]["mean"] == pytest.approx(float(rig.data[1, 10:30, 12:40].mean()), rel=1e-5)


def _ellipse_area(r0, c0, r1, c1):
    a, b = (r1 - r0) / 2, (c1 - c0) / 2
    return math.pi * a * b, 2 * math.pi * math.sqrt((a * a + b * b) / 2)


def test_an_ellipse_has_its_area_within_the_pixel_quantisation(panel, rig):
    _draw_and_add(panel, rig, "ellipse", _rect(8.3, 10.2, 40.7, 55.9))

    true_area, perimeter = _ellipse_area(8.3, 10.2, 40.7, 55.9)
    row = _row(panel)
    assert row["type"] == "ellipse"
    # Each pixel the outline crosses is wrong by less than one pixel, and the
    # outline crosses about as many pixels as its length.
    assert abs(row["area_px"] - true_area) <= perimeter
    assert row["area_px"] > 0.9 * true_area
    assert math.isfinite(row["mean"])


def test_a_polygon_has_its_area_within_the_pixel_quantisation(panel, rig):
    vertices = [[5.2, 6.1], [7.4, 50.3], [44.8, 33.9], [30.1, 8.7]]
    _draw_and_add(panel, rig, "polygon", vertices)

    rows, cols = np.array(vertices)[:, 0], np.array(vertices)[:, 1]
    true_area = 0.5 * abs(np.dot(rows, np.roll(cols, -1)) - np.dot(cols, np.roll(rows, -1)))
    perimeter = float(np.sum(np.hypot(rows - np.roll(rows, -1), cols - np.roll(cols, -1))))
    row = _row(panel)
    assert row["type"] == "polygon"
    assert abs(row["area_px"] - true_area) <= perimeter


def test_points_are_added_as_one_multipoint_roi(panel, rig):
    panel.shapeCombo.setCurrentIndex([m for _l, m in panel.DRAW_MODES].index("point"))
    panel._startDrawing()
    rig.service.add_point(10, 12)
    rig.service.add_point(20, 30)
    panel.add_current_rectangle()
    [roi] = panel.rois()
    assert roi.roi_type == "multipoint"
    assert sorted(roi.vertices) == [(10.0, 12.0), (20.0, 30.0)]


def test_the_roi_follows_the_active_window(qtbot, panel, rig):
    _draw_and_add(panel, rig, "rectangle", _rect(10, 12, 30, 40))
    first_items = panel._overlay._items
    other = rig.manager.show(ArrayProcessingResult("other", np.ones((50, 50), np.float32), ["Y", "X"]))
    qtbot.addWidget(other)

    # Measured on the new window's image, drawn on the new window.
    assert _row(panel)["mean"] == pytest.approx(1.0)
    assert panel._overlay._items and _on(other, panel._overlay._items[0])
    assert not _on(rig.window, first_items[0])

    rig.manager.show(rig.window.result)
    assert _row(panel)["mean"] == pytest.approx(float(rig.data[0, 10:30, 12:40].mean()), rel=1e-5)
    assert _on(rig.window, panel._overlay._items[0])


def test_rois_survive_a_json_round_trip_unchanged(tmp_path, panel, rig):
    from imswitch.imcommon.algorithms.roi_set_io import read_set, write_set  # noqa: PLC0415

    _draw_and_add(panel, rig, "ellipse", _rect(8, 10, 40, 55))
    path = tmp_path / "rois.json"
    write_set(path, panel.active_set())
    loaded = read_set(path)
    assert [(r.roi_type, r.vertices) for r in loaded.rois] == [
        (r.roi_type, r.vertices) for r in panel.rois()
    ]


def test_freehand_stays_visible_but_disabled_with_its_reason(panel):
    index = [m for _l, m in panel.DRAW_MODES].index("path")
    item = panel.shapeCombo.model().item(index)
    assert panel.shapeCombo.itemText(index) == "Freehand"
    assert not item.isEnabled()
    assert panel.shapeCombo.itemData(index, QtCore.Qt.ToolTipRole) == \
        "Not yet supported with separate windows"
    # Every drawable shape stays enabled.
    for row, (_label, mode) in enumerate(panel.DRAW_MODES):
        if mode != "path":
            assert panel.shapeCombo.model().item(row).isEnabled()


def test_a_mode_the_window_cannot_draw_is_reported_not_swallowed(panel):
    panel.shapeCombo.setCurrentIndex([m for _l, m in panel.DRAW_MODES].index("path"))
    panel._startDrawing()
    assert "not supported with separate windows" in panel.summaryLabel.text()


# --------------------------------------------------------------------------
# ROI statistics
# --------------------------------------------------------------------------

def test_roi_stats_measure_the_drawn_rectangle_on_the_current_plane(qtbot, rig):
    widget = ROIStatsWidget(rig.viewer)
    qtbot.addWidget(widget)
    widget.modeCombo.setCurrentText("Rectangle ROI")

    rig.service.add_shape("rectangle", _rect(10, 12, 30, 40))
    region = rig.data[0, 10:30, 12:40]
    assert widget._current_stats.area_pixels == region.size
    assert widget._current_stats.mean == pytest.approx(float(region.mean()), rel=1e-5)

    rig.window.moveDimStep(0, 3)
    assert widget._current_stats.mean == pytest.approx(
        float(rig.data[3, 10:30, 12:40].mean()), rel=1e-5)

    widget.modeCombo.setCurrentText("Full image")
    assert widget._current_stats.mean == pytest.approx(float(rig.data[3].mean()), rel=1e-5)


def test_roi_stats_follow_the_active_window(qtbot, rig):
    widget = ROIStatsWidget(rig.viewer)
    qtbot.addWidget(widget)
    rig.manager.show(ArrayProcessingResult("other", np.full((20, 20), 5.0, np.float32), ["Y", "X"]))
    assert widget._current_stats.mean == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def test_a_line_profile_follows_the_drawn_line_on_the_current_plane(qtbot, rig):
    # A linear image is sampled exactly by bilinear interpolation, so the
    # profile must equal the same linear function along the line. Each plane
    # has its own offset, so reading the wrong plane would show.
    rows, cols = np.mgrid[0:ROWS, 0:COLS].astype(np.float32)
    ramp = np.stack([2.0 * rows + 3.0 * cols + 1000.0 * z for z in range(PLANES)])
    other = rig.manager.show(ArrayProcessingResult("ramp", ramp, ["Z", "Y", "X"]))
    qtbot.addWidget(other)
    widget = ProfileWidget(rig.viewer)
    qtbot.addWidget(widget)

    widget.selectMode("line")
    other.moveDimStep(0, 2)
    rig.service.add_shape("line", [[5.0, 4.0], [40.0, 60.0]])

    [record] = widget._current_record_inputs
    kind, distance, profile, length_px = record[:4]
    assert kind == "line"
    count = profile.size
    r = np.linspace(5.0, 40.0, count)
    c = np.linspace(4.0, 60.0, count)
    assert np.allclose(profile, 2.0 * r + 3.0 * c + 2000.0, atol=1e-3)
    assert length_px == pytest.approx(math.hypot(35.0, 56.0))
    assert distance[-1] == pytest.approx(length_px)

    other.moveDimStep(0, 3)   # the profile re-reads the new plane
    [record3] = widget._current_record_inputs
    assert np.allclose(record3[2], profile + 1000.0, atol=1e-3)


def test_the_layer_chooser_is_disabled_with_its_reason(qtbot, rig):
    widget = ProfileWidget(rig.viewer)
    qtbot.addWidget(widget)
    assert not widget.layerCombo.isEnabled()
    assert widget.layerCombo.toolTip() == "Not yet supported with separate windows"


def test_the_rectangle_profile_works_too(qtbot, rig):
    widget = ProfileWidget(rig.viewer)
    qtbot.addWidget(widget)
    widget.selectMode("rectangle")
    rig.service.add_shape("rectangle", _rect(10, 12, 30, 40))
    assert widget._last_kind == "rectangle"
    region = rig.data[0, 10:30, 12:40]
    by_name = {name: y for name, _x, y in widget._last_payload}
    assert len(widget._last_payload) == 2
    means = sorted(float(v) for y in by_name.values() for v in [np.mean(y)])
    assert means == pytest.approx([float(region.mean())] * 2, rel=1e-5)


# --------------------------------------------------------------------------
# what stays unsupported, and how it says so
# --------------------------------------------------------------------------

def test_the_supported_tools_are_listed_and_the_rest_are_not():
    assert window_mode.SUPPORTED_IN_WINDOW_MODE == {
        "profile", "roi-manager", "roi-stats", "crop-substack",
    }
    assert {tool.id for tool in window_mode.unsupported_tools()} == {
        "segmentation", "psf-resolution", "colocalization", "multicolor",
        "napari-plugins",
    }
    assert window_mode.panel_tool("roi-manager") is None
    assert window_mode.panel_tool("segmentation") is not None


def test_a_generic_result_processor_panel_is_restricted_by_its_own_id(monkeypatch):
    """The panel kind of every processor tool is "result-processor", which names nothing."""
    tool = window_mode.NapariOnlyTool("some-processor", "Some", "panel", "somePanel")
    monkeypatch.setattr(window_mode, "NAPARI_ONLY_TOOLS", (*window_mode.NAPARI_ONLY_TOOLS, tool))

    assert window_mode.panel_tool("result-processor", "some-processor") is tool
    assert window_mode.panel_tool("result-processor", "denoise") is None
    assert window_mode.panel_tool("result-processor", "stack-subset") is None


def test_a_restricted_panel_shows_its_reason_everywhere_the_pointer_can_rest(qtbot):
    panel = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(panel)
    button = QtWidgets.QPushButton("Run")
    button.setToolTip("Runs the thing")
    layout.addWidget(button)
    layout.addWidget(QtWidgets.QLabel("plain"))
    dock = Dock("Segmentation")
    dock.addWidget(panel)
    qtbot.addWidget(dock)
    action = QtWidgets.QAction("Segmentation")
    window_mode.restrict_panel(panel, {"Segmentation": action})

    assert not panel.isEnabled()
    assert panel.toolTip() == window_mode.UNSUPPORTED_TOOLTIP
    assert button.toolTip() == window_mode.UNSUPPORTED_TOOLTIP     # its own text replaced
    assert all(child.toolTip() == window_mode.UNSUPPORTED_TOOLTIP
               for child in panel.findChildren(QtWidgets.QWidget))
    assert dock.label.toolTip() == window_mode.UNSUPPORTED_TOOLTIP
    assert action.toolTip() == window_mode.UNSUPPORTED_TOOLTIP


def test_the_view_puts_the_reason_on_the_dock_the_view_entry_and_the_tools_action(qtbot):
    from types import SimpleNamespace

    from imswitch.improcess.view.ImProcessMainView import _restrict_panel_for_window_mode

    panel = QtWidgets.QWidget()
    dock = Dock("Segmentation")
    dock.addWidget(panel)
    qtbot.addWidget(dock)
    view_entry, tools_action = QtWidgets.QAction("Segmentation"), QtWidgets.QAction("Segmentation")
    view = SimpleNamespace(
        segmentationWidget=panel,
        _dockVisibilityActions={"Segmentation": view_entry},
        _analysisToolActions={"segmentation": tools_action},
    )
    tool = next(t for t in window_mode.unsupported_tools() if t.id == "segmentation")

    _restrict_panel_for_window_mode(view, tool)

    for text in (panel.toolTip(), dock.label.toolTip(), view_entry.toolTip(),
                 tools_action.toolTip(), tools_action.statusTip()):
        assert text == window_mode.UNSUPPORTED_TOOLTIP
    assert not panel.isEnabled()

"""Widget-level P-0 fixes for the ROI manager.

Uses the fake-viewer pattern the other ImProcess panel tests use, so no napari
viewer or display is needed.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import (  # noqa: E402
    ROI_KEY_ROLE,
    ROIManagerWidget,
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class _Event:
    def __init__(self):
        self._handlers = []

    def connect(self, fn):
        self._handlers.append(fn)

    def disconnect(self, fn):
        if fn in self._handlers:
            self._handlers.remove(fn)

    def emit(self, *args):
        for fn in list(self._handlers):
            fn(*args)


class _Shapes:
    def __init__(self, **kwargs):
        self._data, self.shape_type, self.mode = [], [], "pan_zoom"
        self.name = kwargs.get("name", "Viewer Tools")
        self.visible, self.edge_width = True, 1.0
        self.current_edge_width = 1.0
        self.editable = True
        self.edge_color, self.face_color = [], []
        self.features, self.text = {}, None
        self.scale, self.translate = (1.0, 1.0), (0.0, 0.0)
        self.mouse_drag_callbacks = []
        self.events = SimpleNamespace(data=_Event(), mode=_Event())

    @property
    def data(self):
        return list(self._data)

    @data.setter
    def data(self, value):
        value = list(value)
        self.shape_type = self.shape_type[: len(value)]
        self._data = value
        self.events.data.emit(SimpleNamespace(value=value))

    def add_shape(self, vertices, shape_type):
        self._data.append(np.asarray(vertices, dtype=float))
        self.shape_type.append(shape_type)
        self.events.data.emit(SimpleNamespace(value=self._data))

    def world_to_data(self, point):
        return np.asarray(point, dtype=float)


class _Points:
    """The parts of a napari Points layer the tool manager touches."""

    def __init__(self, **kwargs):
        self._data = []
        self.name = kwargs.get("name", "Viewer Tool Points")
        self.mode = "pan_zoom"
        self.visible, self.ndim = True, 2
        self.size = kwargs.get("size", 8)
        self.scale, self.translate = (1.0, 1.0), (0.0, 0.0)
        self.events = SimpleNamespace(data=_Event())

    @property
    def data(self):
        return list(self._data)

    @data.setter
    def data(self, value):
        self._data = [np.asarray(point, dtype=float) for point in value]
        self.events.data.emit(SimpleNamespace(value=self._data))


class _Layer:
    def __init__(self, name, data):
        self.name, self.data = name, data
        self.scale = tuple(1.0 for _ in range(data.ndim))
        self.visible, self.ndim = True, data.ndim
        self.metadata = {"scale_unit": "px", "axis_labels": ["Y", "X"]}


class _Layers(list):
    def __init__(self):
        super().__init__()
        self.selection = SimpleNamespace(active=None)


class _Viewer:
    def __init__(self, image):
        self.layers = _Layers()
        layer = _Layer("Reconstruction", image)
        self.layers.append(layer)
        self.layers.selection.active = layer
        self.camera = SimpleNamespace(zoom=1.0, events=SimpleNamespace(zoom=_Event()))
        self.dims = SimpleNamespace(
            events=SimpleNamespace(current_step=_Event()),
            ndisplay=2,
            current_step=(0, 0),
            axis_labels=("Y", "X"),
        )

    def add_shapes(self, **kwargs):
        layer = _Shapes(**kwargs)
        self.layers.append(layer)
        return layer

    def add_points(self, **kwargs):
        layer = _Points(**kwargs)
        self.layers.append(layer)
        return layer


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.ones((16, 16), dtype=float)))
    yield widget
    widget.deleteLater()


def _row_named(widget, name):
    """The row displaying ``name``.

    Found via the Name column: rows are keyed by uid, precisely so a display
    name is never what an action resolves through.
    """
    column = widget.column_index("Name")
    for row in range(widget.table.rowCount()):
        item = widget.table.item(row, column)
        if item is not None and item.text() == name:
            return row
    return None


def _row_uid(widget, row):
    item = widget.table.item(row, 0)
    return item.data(ROI_KEY_ROLE) if item is not None else None


# --------------------------------------------------------------------------
# D-01 / Q-04(b) — hidden ROIs stay in the table
# --------------------------------------------------------------------------

def test_hidden_roi_keeps_its_row_and_checkbox(panel):
    """Unchecking Visible must not remove the checkbox that undoes it."""
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    assert panel.table.rowCount() == 1

    row = _row_named(panel, "cell")
    panel.table.item(row, 0).setCheckState(QtCore.Qt.Unchecked)

    row = _row_named(panel, "cell")
    assert row is not None, "the hidden ROI lost its row"
    item = panel.table.item(row, 0)
    assert item.checkState() == QtCore.Qt.Unchecked
    assert item.flags() & QtCore.Qt.ItemIsUserCheckable

    # ...and it can be turned back on
    item.setCheckState(QtCore.Qt.Checked)
    assert panel.rois(visible_only=True)[0].name == "cell"


def test_hidden_roi_statistics_are_blank(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    area_column = panel.column_index("Area")
    row = _row_named(panel, "cell")
    assert panel.table.item(row, area_column).text() == "16"

    panel.table.item(row, 0).setCheckState(QtCore.Qt.Unchecked)

    row = _row_named(panel, "cell")
    assert panel.table.item(row, area_column).text() == ""
    assert panel.table.item(row, panel.column_index("Note")).text() == "hidden"


def test_rois_visible_only_keyword_is_opt_in(panel):
    panel.add_rois(
        [
            ROIRecord("shown", "rectangle", (0, 4, 0, 4)),
            ROIRecord("hidden", "rectangle", (4, 8, 4, 8), visible=False),
        ]
    )

    assert [roi.name for roi in panel.rois()] == ["shown", "hidden"]
    assert [roi.name for roi in panel.rois(visible_only=True)] == ["shown"]


# --------------------------------------------------------------------------
# D-03 — one bad ROI must not blank the table
# --------------------------------------------------------------------------

def test_out_of_bounds_roi_does_not_blank_the_table(panel):
    panel.add_rois(
        [
            ROIRecord("good", "rectangle", (0, 4, 0, 4)),
            ROIRecord("outside", "rectangle", (100, 120, 100, 120)),
        ]
    )

    assert panel.table.rowCount() == 2
    area_column = panel.column_index("Area")
    assert panel.table.item(_row_named(panel, "good"), area_column).text() == "16"
    assert panel.table.item(_row_named(panel, "outside"), panel.column_index("Note")).text()


# --------------------------------------------------------------------------
# D-05 — actions target the clicked row, not the row index
# --------------------------------------------------------------------------

def test_selection_after_sorting_targets_the_clicked_roi(panel):
    panel.add_rois(
        [
            ROIRecord("alpha", "rectangle", (0, 2, 0, 2)),
            ROIRecord("beta", "rectangle", (0, 8, 0, 8)),
        ]
    )

    # Sort descending by name so display order is the reverse of model order.
    name_column = panel.column_index("Name")
    panel.table.sortItems(name_column, QtCore.Qt.DescendingOrder)
    assert panel.table.item(0, name_column).text() == "beta"

    panel.table.setCurrentCell(0, name_column)
    selected = panel._selected_roi()

    assert selected is not None
    assert selected.name == "beta", "row 0 resolved to the wrong ROI after sorting"


def test_delete_after_sorting_removes_the_clicked_roi(panel):
    panel.add_rois(
        [
            ROIRecord("alpha", "rectangle", (0, 2, 0, 2)),
            ROIRecord("beta", "rectangle", (0, 8, 0, 8)),
        ]
    )
    name_column = panel.column_index("Name")
    panel.table.sortItems(name_column, QtCore.Qt.DescendingOrder)
    panel.table.setCurrentCell(0, name_column)

    panel.delete_selected()

    assert [roi.name for roi in panel.rois()] == ["alpha"]


def test_numeric_columns_sort_numerically(panel):
    """Sorting Area must order 9 before 100, not lexically."""
    panel.add_rois(
        [
            ROIRecord("big", "rectangle", (0, 10, 0, 10)),   # area 100
            ROIRecord("small", "rectangle", (0, 3, 0, 3)),   # area 9
        ]
    )

    area_column = panel.column_index("Area")
    panel.table.sortItems(area_column, QtCore.Qt.AscendingOrder)

    assert panel.table.item(0, area_column).text() == "9"
    assert panel.table.item(1, area_column).text() == "100"


# --------------------------------------------------------------------------
# rows key on uid, and mutations go through the command log (round 6, P1)
# --------------------------------------------------------------------------

def test_rows_are_keyed_by_uid_not_name(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])

    row = _row_named(panel, "cell")
    uid = _row_uid(panel, row)

    assert uid and uid == panel.rois()[0].uid
    assert uid != "cell"


def test_renaming_keeps_the_row_identity(panel):
    """The uid is what the row resolves through, so it survives a rename."""
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    before = _row_uid(panel, _row_named(panel, "cell"))

    panel._model.rename("cell", "nucleus")
    panel.refresh_stats()

    assert _row_uid(panel, _row_named(panel, "nucleus")) == before


def test_widget_mutations_are_recorded_as_undoable_commands(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    assert panel._commands.can_undo

    panel._commands.undo()

    assert panel.rois() == []


def test_delete_through_the_widget_is_undoable(panel):
    panel.add_rois(
        [
            ROIRecord("a", "rectangle", (0, 2, 0, 2)),
            ROIRecord("b", "rectangle", (2, 6, 2, 6)),
        ]
    )
    panel.table.setCurrentCell(_row_named(panel, "a"), 1)
    panel.delete_selected()
    assert [roi.name for roi in panel.rois()] == ["b"]

    panel._commands.undo()

    assert [roi.name for roi in panel.rois()] == ["a", "b"]


def test_update_refuses_to_change_identity_or_collide_on_name():
    from imswitch.improcess.analysis.roi_manager import ROIManagerModel

    model = ROIManagerModel(
        [
            ROIRecord("a", "rectangle", (0, 2, 0, 2)),
            ROIRecord("b", "rectangle", (2, 4, 2, 4)),
        ]
    )

    with pytest.raises(ValueError):
        model.update("a", uid="something-else")
    with pytest.raises(ValueError):
        model.update("a", name="b")


# --------------------------------------------------------------------------
# P-2.4 at the widget level: every drawn shape is captured (D-08)
# --------------------------------------------------------------------------

def _start_drawing(panel, mode):
    """Pick a shape in the chooser and take the tool, as the user would."""
    index = [m for _label, m in panel.DRAW_MODES].index(mode)
    panel.shapeCombo.setCurrentIndex(index)
    panel._startDrawing()


def _draw(panel, *rects):
    """Draw rectangles into the panel's scratch layer, as the user would."""
    _start_drawing(panel, "rectangle")
    layer = panel._toolService.manager.get_layer()
    for r0, c0, r1, c1 in rects:
        layer.add_shape([[r0, c0], [r0, c1], [r1, c1], [r1, c0]], "rectangle")


def test_all_drawn_shapes_are_added(panel):
    """Three rectangles drawn, three ROIs added.

    The broker's one-shape-per-owner rule made this impossible until the ROI
    manager opted out of it: the panel captured one of three and said nothing
    about the other two.
    """
    _draw(panel, (0, 0, 4, 4), (5, 5, 9, 9), (10, 10, 14, 14))

    panel.add_current_rectangle()

    assert len(panel.rois()) == 3, [roi.name for roi in panel.rois()]


def test_capturing_clears_only_this_panels_scratch_shapes(panel):
    _draw(panel, (0, 0, 4, 4), (5, 5, 9, 9))

    panel.add_current_rectangle()

    assert panel._toolService.shapes(panel._toolToken) == []


def test_the_frame_is_registered_on_the_set_once(panel):
    """P-2.7: the set keeps the frame, so it is self-describing."""
    _draw(panel, (0, 0, 4, 4))
    panel.add_current_rectangle()
    _draw(panel, (5, 5, 9, 9))
    panel.add_current_rectangle()

    assert len(panel._set.frames) == 1, "the frame should be stored once per set"
    assert panel.rois()[0].frame_uid == panel._set.frames[0].frame_uid


def test_the_panel_measures_the_brokers_target_layer(panel):
    """Not whatever active_image_layer() happens to return after a click."""
    assert panel._toolService.target_image_layer is not None
    assert panel._active_image_layer() is panel._toolService.target_image_layer

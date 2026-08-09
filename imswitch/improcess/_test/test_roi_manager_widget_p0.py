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

class _Shapes:
    def __init__(self, **kwargs):
        self.data, self.shape_type, self.mode = [], [], "pan_zoom"
        self.name = kwargs.get("name", "Viewer Tools")
        self.visible, self.edge_width = True, 1.0
        event = lambda: SimpleNamespace(connect=lambda _fn: None)  # noqa: E731
        self.events = SimpleNamespace(data=event(), mode=event())


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
        self.camera = SimpleNamespace(zoom=1.0)
        self.add_shapes = lambda **kwargs: _Shapes(**kwargs)
        self.dims = SimpleNamespace(
            events=SimpleNamespace(current_step=SimpleNamespace(connect=lambda _fn: None)),
            ndisplay=2,
            current_step=(0, 0),
        )


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
    for row in range(widget.table.rowCount()):
        item = widget.table.item(row, 0)
        if item is not None and item.data(ROI_KEY_ROLE) == name:
            return row
    return None


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
    area_column = panel._COLUMNS.index("Area")
    row = _row_named(panel, "cell")
    assert panel.table.item(row, area_column).text() == "16"

    panel.table.item(row, 0).setCheckState(QtCore.Qt.Unchecked)

    row = _row_named(panel, "cell")
    assert panel.table.item(row, area_column).text() == ""
    assert panel.table.item(row, panel._COLUMNS.index("Note")).text() == "hidden"


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
    area_column = panel._COLUMNS.index("Area")
    assert panel.table.item(_row_named(panel, "good"), area_column).text() == "16"
    assert panel.table.item(_row_named(panel, "outside"), panel._COLUMNS.index("Note")).text()


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
    name_column = panel._COLUMNS.index("Name")
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
    name_column = panel._COLUMNS.index("Name")
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

    area_column = panel._COLUMNS.index("Area")
    panel.table.sortItems(area_column, QtCore.Qt.AscendingOrder)

    assert panel.table.item(0, area_column).text() == "9"
    assert panel.table.item(1, area_column).text() == "100"

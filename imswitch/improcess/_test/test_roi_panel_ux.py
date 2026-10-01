"""P-7: the rest of ImageJ's ROI Manager — Update, Specify, Properties, Sort,
Deselect, the filter, the context menu and the shortcut set."""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_style import ROIStyle  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402
from imswitch.improcess.view.ROIPropertiesDialog import (  # noqa: E402
    ROIPropertiesDialog,
    ROISpecifyDialog,
)

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.arange(256, dtype=float).reshape(16, 16)))
    yield widget
    widget.deleteLater()


def _select(panel, *names):
    panel.table.clearSelection()
    column = panel.column_index("Name")
    for row in range(panel.table.rowCount()):
        item = panel.table.item(row, column)
        if item is not None and item.text() in names:
            panel.table.selectRow(row)


# --------------------------------------------------------------------------
# P-7.2 — Deselect
# --------------------------------------------------------------------------

def test_deselect_clears_the_selection(panel):
    panel.add_rois([
        ROIRecord("a", "rectangle", (0, 4, 0, 4)),
        ROIRecord("b", "rectangle", (8, 12, 8, 12)),
    ])
    panel.table.selectAll()
    assert len(panel._selected_rois()) == 2

    panel.deselect()
    assert panel._selected_rois() == []


def test_deselecting_makes_measure_act_on_everything_again(panel):
    """Which is why ImageJ has it as a button rather than a click on nothing."""
    panel.add_rois([
        ROIRecord("a", "rectangle", (0, 4, 0, 4)),
        ROIRecord("b", "rectangle", (8, 12, 8, 12)),
    ])
    _select(panel, "a")
    pushed = []
    panel.sigResultPushed.connect(lambda columns, rows: pushed.append(rows))

    panel.measure()
    assert [row["roi"] for row in pushed[-1]] == ["a"]

    panel.deselect()
    panel.measure()
    assert [row["roi"] for row in pushed[-1]] == ["a", "b"]


# --------------------------------------------------------------------------
# P-7.4 — Update
# --------------------------------------------------------------------------

def test_update_replaces_geometry_and_keeps_identity(panel):
    """The same region, moved — so rows pushed earlier still refer to it."""
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    original = panel._model.rois[0]
    _select(panel, "cell")

    layer = panel._toolService._manager
    layer._ensure_shapes_layer()
    layer._shapes_layer.add_shape(
        [(8.0, 8.0), (8.0, 12.0), (12.0, 12.0), (12.0, 8.0)], "rectangle"
    )
    panel._toolService.claim_new_shapes(panel._toolToken)

    panel.update_selected()
    updated = panel._model.get("cell")
    assert updated.uid == original.uid          # the same ROI
    assert updated.bounds != original.bounds    # somewhere else
    assert updated.revision == original.revision + 1


def test_update_is_undoable(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    _select(panel, "cell")
    layer = panel._toolService._manager
    layer._ensure_shapes_layer()
    layer._shapes_layer.add_shape(
        [(8.0, 8.0), (8.0, 12.0), (12.0, 12.0), (12.0, 8.0)], "rectangle"
    )
    panel._toolService.claim_new_shapes(panel._toolToken)
    panel.update_selected()

    panel.undo()
    assert panel._model.get("cell").bounds == (0, 4, 0, 4)


def test_update_with_nothing_drawn_says_so(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    _select(panel, "cell")
    panel.update_selected()
    assert panel._model.get("cell").bounds == (0, 4, 0, 4)
    assert "shape" in panel.summaryLabel.text().lower()


# --------------------------------------------------------------------------
# P-7.4 — Sort
# --------------------------------------------------------------------------

def test_sort_orders_the_model_not_just_the_view(panel):
    """The model's order is what is exported, saved and measured in order."""
    panel.add_rois([
        ROIRecord("charlie", "rectangle", (0, 4, 0, 4)),
        ROIRecord("alpha", "rectangle", (5, 9, 5, 9)),
        ROIRecord("bravo", "rectangle", (10, 14, 10, 14)),
    ])
    panel.sort_rois()
    assert [roi.name for roi in panel._model.rois] == ["alpha", "bravo", "charlie"]


def test_sorting_an_already_sorted_list_does_nothing(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    before = len(panel._commands.labels())
    panel.sort_rois()
    assert len(panel._commands.labels()) == before
    assert "Already in name order" in panel.summaryLabel.text()


def test_sort_is_undoable(panel):
    panel.add_rois([
        ROIRecord("charlie", "rectangle", (0, 4, 0, 4)),
        ROIRecord("alpha", "rectangle", (5, 9, 5, 9)),
    ])
    panel.sort_rois()
    panel.undo()
    assert [roi.name for roi in panel._model.rois] == ["charlie", "alpha"]


# --------------------------------------------------------------------------
# P-7.4 — Specify
# --------------------------------------------------------------------------

def test_specify_creates_an_roi_at_exact_coordinates(panel, monkeypatch):
    monkeypatch.setattr(
        ROISpecifyDialog, "specify",
        staticmethod(lambda *a, **k: ("rectangle", (3, 11, 5, 17))),
    )
    panel.specify_roi()

    assert len(panel._model.rois) == 1
    assert panel._model.rois[0].bounds == (3, 11, 5, 17)
    assert panel._model.rois[0].source == "specified"


def test_specify_centres_on_the_image_when_asked(qapp):
    dialog = ROISpecifyDialog(shape=(100, 200))
    dialog.heightSpin.setValue(10)
    dialog.widthSpin.setValue(20)
    dialog.centeredCheck.setChecked(True)
    try:
        assert dialog.bounds() == (45, 55, 90, 110)
    finally:
        dialog.deleteLater()


def test_specify_bounds_are_half_open_like_every_other_roi(qapp):
    dialog = ROISpecifyDialog(shape=(100, 100))
    dialog.topSpin.setValue(10)
    dialog.leftSpin.setValue(20)
    dialog.heightSpin.setValue(5)
    dialog.widthSpin.setValue(7)
    try:
        r0, r1, c0, c1 = dialog.bounds()
        assert (r1 - r0, c1 - c0) == (5, 7)
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# P-7.4 — Properties
# --------------------------------------------------------------------------

def test_properties_of_one_roi_offers_its_current_style(qapp):
    roi = ROIRecord(
        "a", "rectangle", (0, 4, 0, 4), group=3,
        style=ROIStyle(stroke_color="#ff0000", stroke_width=2.0),
    )
    dialog = ROIPropertiesDialog([roi])
    try:
        assert dialog.nameEdit.text() == "a"
        assert dialog.groupSpin.value() == 3
        assert dialog.strokeColor.color() == "#ff0000"
    finally:
        dialog.deleteLater()


def test_an_untouched_batch_dialog_changes_nothing(qapp):
    """Otherwise every ROI would take the first one's appearance."""
    dialog = ROIPropertiesDialog([
        ROIRecord("a", "rectangle", (0, 4, 0, 4), style=ROIStyle(stroke_color="#f00")),
        ROIRecord("b", "rectangle", (5, 9, 5, 9), style=ROIStyle(stroke_color="#0f0")),
    ])
    try:
        assert dialog.changes() == {}
    finally:
        dialog.deleteLater()


def test_a_batch_change_sets_only_the_field_that_was_touched(qapp):
    dialog = ROIPropertiesDialog([
        ROIRecord("a", "rectangle", (0, 4, 0, 4)),
        ROIRecord("b", "rectangle", (5, 9, 5, 9)),
    ])
    try:
        dialog.groupSpin.setValue(7)
        assert dialog.changes() == {"group": 7}
    finally:
        dialog.deleteLater()


def test_properties_merges_a_style_rather_than_replacing_it(panel, monkeypatch):
    """A batch setting the width must not flatten two different colours."""
    panel.add_rois([
        ROIRecord("a", "rectangle", (0, 4, 0, 4), style=ROIStyle(stroke_color="#ff0000")),
        ROIRecord("b", "rectangle", (5, 9, 5, 9), style=ROIStyle(stroke_color="#00ff00")),
    ])
    panel.table.selectAll()
    monkeypatch.setattr(
        ROIPropertiesDialog, "edit",
        classmethod(lambda cls, rois, parent=None: {"_style": {"stroke_width": 3.0}}),
    )
    panel.edit_properties()

    assert panel._model.get("a").style.stroke_color == "#ff0000"
    assert panel._model.get("b").style.stroke_color == "#00ff00"
    assert panel._model.get("a").style.stroke_width == 3.0
    assert panel._model.get("b").style.stroke_width == 3.0


def test_properties_does_not_offer_geometry(qapp):
    """It cannot be the thing that silently moves a region."""
    dialog = ROIPropertiesDialog([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    try:
        names = {
            child.objectName() for child in dialog.findChildren(QtWidgets.QWidget)
        }
        assert not any("bound" in name.lower() for name in names)
        assert not hasattr(dialog, "topSpin")
    finally:
        dialog.deleteLater()


def test_a_style_change_does_not_invalidate_a_measurement(panel, monkeypatch):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    before = panel._model.rois[0].revision
    panel.table.selectAll()
    monkeypatch.setattr(
        ROIPropertiesDialog, "edit",
        classmethod(lambda cls, rois, parent=None: {"_style": {"stroke_color": "#123456"}}),
    )
    panel.edit_properties()
    assert panel._model.rois[0].revision == before


# --------------------------------------------------------------------------
# P-7.5 — the filter
# --------------------------------------------------------------------------

def test_the_filter_hides_rows_without_removing_anything(panel):
    """A filter that deleted would delete things the user cannot see."""
    panel.add_rois([
        ROIRecord("cell_1", "rectangle", (0, 4, 0, 4)),
        ROIRecord("cell_2", "rectangle", (5, 9, 5, 9)),
        ROIRecord("background", "rectangle", (10, 14, 10, 14)),
    ])
    panel.filterEdit.setText("cell")

    hidden = [panel.table.isRowHidden(row) for row in range(panel.table.rowCount())]
    assert hidden.count(True) == 1
    assert len(panel._model.rois) == 3


def test_the_filter_survives_a_table_rebuild(panel):
    panel.add_rois([
        ROIRecord("cell", "rectangle", (0, 4, 0, 4)),
        ROIRecord("background", "rectangle", (5, 9, 5, 9)),
    ])
    panel.filterEdit.setText("cell")
    panel.refresh_stats()

    column = panel.column_index("Name")
    for row in range(panel.table.rowCount()):
        name = panel.table.item(row, column).text()
        assert panel.table.isRowHidden(row) == (name != "cell")


def test_clearing_the_filter_shows_everything_again(panel):
    panel.add_rois([
        ROIRecord("cell", "rectangle", (0, 4, 0, 4)),
        ROIRecord("background", "rectangle", (5, 9, 5, 9)),
    ])
    panel.filterEdit.setText("cell")
    panel.filterEdit.setText("")
    assert not any(
        panel.table.isRowHidden(row) for row in range(panel.table.rowCount())
    )


# --------------------------------------------------------------------------
# P-7.6 — the context menu
# --------------------------------------------------------------------------

def test_the_table_has_a_context_menu(panel):
    assert panel.table.contextMenuPolicy() == QtCore.Qt.CustomContextMenu


# --------------------------------------------------------------------------
# P-7.3 — shortcuts
# --------------------------------------------------------------------------

def test_the_imagej_shortcut_set_is_catalogued():
    from imswitch.improcess.controller.shortcuts import improcess_shortcut_defaults

    defaults = improcess_shortcut_defaults()
    assert defaults["roi.add"] == "T"          # ImageJ's Add
    assert defaults["roi.delete"] == "Del"
    assert defaults["roi.rename"] == "F2"
    assert defaults["roi.undo"] == "Ctrl+Z"

    bound = [key for key in defaults.values() if key]
    assert len(bound) == len(set(bound)), sorted(bound)


def test_every_roi_shortcut_reaches_a_method_that_exists(panel):
    """A callback naming a method that does not exist fails only when pressed."""
    from imswitch.improcess.controller.shortcuts import _ROI_SPECS

    for _action_id, _name, _key, method_name in _ROI_SPECS:
        assert callable(getattr(panel, method_name, None)), method_name

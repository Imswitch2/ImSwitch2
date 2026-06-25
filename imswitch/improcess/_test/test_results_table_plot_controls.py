"""Plot-control-row behaviour on the ResultsTableWidget.

Constructs the real Qt widget under an offscreen QApplication, but imports it
by path to avoid the view package's matplotlib/napari import chain (mirrors
``test_results_table.py``).
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from ResultsTableWidget import ResultsTableWidget
finally:
    sys.path.pop(0)

from qtpy import QtWidgets


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def widget(qapp):
    w = ResultsTableWidget(show_filter=True, show_csv=True, show_plot=True)
    w.set_records(
        ["area", "ecc", "label"],
        [
            {"area": 1.0, "ecc": 0.1, "label": "a"},
            {"area": 2.0, "ecc": 0.2, "label": "b"},
            {"area": 3.0, "ecc": 0.3, "label": "c"},
        ],
    )
    return w


def _select_kind(widget, kind):
    for index, (_label, value) in enumerate(widget._PLOT_TYPES):
        if value == kind:
            widget.plotTypeCombo.setCurrentIndex(index)
            return
    raise AssertionError(f"kind {kind!r} not found")


def test_accessors_and_numeric_columns(widget):
    assert widget.get_columns() == ["area", "ecc", "label"]
    assert len(widget.get_records()) == 3
    # Text column 'label' is excluded.
    assert widget.numeric_columns() == ["area", "ecc"]


def test_plot_combos_only_offer_numeric_columns(widget):
    assert [widget.plotXCombo.itemText(i) for i in range(widget.plotXCombo.count())] == ["area", "ecc"]


def test_histogram_spec(widget):
    specs = []
    widget.sigPlotRequested.connect(specs.append)
    _select_kind(widget, "histogram")
    widget.plotXCombo.setCurrentText("area")
    widget._on_plot_clicked()
    assert specs == [{"kind": "histogram", "column": "area"}]


def test_line_spec_uses_x_and_y(widget):
    specs = []
    widget.sigPlotRequested.connect(specs.append)
    _select_kind(widget, "line")
    widget.plotXCombo.setCurrentText("area")
    widget.plotYCombo.setCurrentText("ecc")
    widget._on_plot_clicked()
    assert specs == [{"kind": "line", "x_column": "area", "y_column": "ecc"}]


def test_hist2d_spec(widget):
    specs = []
    widget.sigPlotRequested.connect(specs.append)
    _select_kind(widget, "hist2d")
    widget.plotXCombo.setCurrentText("ecc")
    widget.plotYCombo.setCurrentText("area")
    widget._on_plot_clicked()
    assert specs == [{"kind": "hist2d", "x_column": "ecc", "y_column": "area"}]


def test_y_combo_hidden_for_histogram_shown_for_xy(widget):
    # isVisibleTo reflects the intended visibility without showing the window.
    _select_kind(widget, "histogram")
    assert not widget.plotYCombo.isVisibleTo(widget)
    _select_kind(widget, "scatter")
    assert widget.plotYCombo.isVisibleTo(widget)


def test_plot_button_disabled_without_numeric_columns(qapp):
    w = ResultsTableWidget(show_plot=True)
    w.set_records(["label"], [{"label": "a"}, {"label": "b"}])
    assert not w.plotButton.isEnabled()

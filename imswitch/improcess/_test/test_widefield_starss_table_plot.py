"""WFS params widget exposes the generic table-plot controls and re-emits the
full accumulated records (not the truncated regions-table preview)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qtpy import QtWidgets


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def widget(qapp):
    try:
        from imswitch.improcess.reconstructors.widefield_starss.params_widget import (
            WidefieldStarssParamsWidget,
        )

        w = WidefieldStarssParamsWidget()
    except Exception as exc:  # local env: view package needs a working napari
        pytest.skip(f"WFS params widget needs the napari view stack: {exc}")
    # More than the 500-row preview cap so we can prove plotting uses the full set.
    region_records = [
        {"pair_index": 0, "area": float(i), "ecc": 0.001 * i} for i in range(600)
    ]
    w.append_results(
        {
            "summary_columns": ["sample_id", "area"],
            "summary_records": [{"sample_id": "s1", "area": 10.0}],
            "region_columns": ["pair_index", "area", "ecc"],
            "region_records": region_records,
        }
    )
    return w


def test_both_tables_have_plot_controls(widget):
    assert hasattr(widget.batchRegionsTable, "plotButton")
    assert hasattr(widget.batchSummaryTable, "plotButton")


def test_regions_replot_emits_full_records(widget):
    captured = []
    widget.sigTablePlotRequested.connect(lambda c, r, s: captured.append((c, r, s)))
    widget.batchRegionsTable.sigPlotRequested.emit({"kind": "histogram", "column": "area"})
    assert len(captured) == 1
    columns, records, spec = captured[0]
    assert columns == ["pair_index", "area", "ecc"]
    # 600 accumulated rows, not the 500-row table preview.
    assert len(records) == 600
    assert spec == {"kind": "histogram", "column": "area"}


def test_summary_replot_emits_summary_records(widget):
    captured = []
    widget.sigTablePlotRequested.connect(lambda c, r, s: captured.append((c, r, s)))
    widget.batchSummaryTable.sigPlotRequested.emit({"kind": "histogram", "column": "area"})
    assert len(captured) == 1
    columns, records, _spec = captured[0]
    assert columns == ["sample_id", "area"]
    assert records == [{"sample_id": "s1", "area": 10.0}]

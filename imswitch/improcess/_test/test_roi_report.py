"""P-4 row, plot and wide-form shapes, with no viewer in sight."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.improcess.analysis.roi_report import (
    IDENTITY_COLUMNS,
    ROW_KIND,
    measurement_row,
    multi_plot_payload,
    rows_to_columns,
    wide_form,
)


def _rows():
    """Two ROIs across three planes — the acceptance criterion's 6 rows."""
    rois = [
        ROIRecord("a", "rectangle", (0, 4, 0, 4), uid="ua"),
        ROIRecord("b", "rectangle", (4, 8, 4, 8), uid="ub"),
    ]
    rows = []
    for z in range(3):
        for index, roi in enumerate(rois):
            rows.append(
                measurement_row(
                    roi,
                    {"mean": float(z * 10 + index), "area_px": 16},
                    source="recon",
                    plane=(("Z", z),),
                )
            )
    return rows


def test_a_row_names_its_roi_its_source_and_its_plane():
    rows = _rows()
    assert len(rows) == 6
    first = rows[0]
    assert first["source"] == "recon"
    assert first["kind"] == ROW_KIND
    assert first["roi"] == "a"
    assert first["roi_uid"] == "ua"
    assert first["Z"] == 0
    assert first["mean"] == 0.0


def test_rows_are_plane_major():
    """Both ROIs on plane 0, then both on plane 1 — as ImageJ reports it."""
    assert [(row["Z"], row["roi"]) for row in _rows()] == [
        (0, "a"), (0, "b"), (1, "a"), (1, "b"), (2, "a"), (2, "b"),
    ]


def test_identity_columns_lead_whatever_the_measurements_are():
    columns = rows_to_columns(_rows())
    # "label" is opt-in (Display label), so it is absent here — the ones that
    # are present keep their pinned order.
    present = [column for column in IDENTITY_COLUMNS if column in columns]
    assert columns[: len(present)] == present
    assert "label" not in columns
    assert columns.index("Z") < columns.index("mean")


def test_columns_cover_rows_that_do_not_all_carry_the_same_keys():
    rows = [{"roi": "a", "mean": 1.0}, {"roi": "b", "circularity": 0.5}]
    assert rows_to_columns(rows) == ["roi", "mean", "circularity"]


def test_multi_plot_makes_one_series_per_roi():
    payload = multi_plot_payload(_rows(), column="mean", axis_label="Z")
    assert [series.name for series in payload.series] == ["a", "b"]
    assert np.allclose(payload.series[0].x, [0, 1, 2])
    assert np.allclose(payload.series[0].y, [0, 10, 20])
    assert payload.x_label == "Z"
    assert payload.y_label == "mean"


def test_multi_plot_labels_the_unit_when_there_is_one():
    payload = multi_plot_payload(_rows(), column="mean", axis_label="Z", unit="um")
    assert payload.y_label == "mean (um)"


def test_multi_plot_refuses_rather_than_drawing_an_empty_graph():
    with pytest.raises(ValueError):
        multi_plot_payload([], column="mean", axis_label="Z")
    rows = [{"roi": "a", "Z": 0, "mean": float("nan")}]
    with pytest.raises(ValueError, match="mean"):
        multi_plot_payload(rows, column="mean", axis_label="Z")


def test_multi_plot_skips_an_roi_with_no_numbers_but_keeps_the_others():
    rows = _rows() + [{"roi": "c", "Z": 0, "mean": None}]
    payload = multi_plot_payload(rows, column="mean", axis_label="Z")
    assert [series.name for series in payload.series] == ["a", "b"]


def test_wide_form_is_one_row_per_plane_and_one_column_per_roi():
    columns, records = wide_form(_rows(), column="mean", axis_label="Z")
    assert columns == ["Z", "a", "b"]
    assert records == [
        {"Z": 0, "a": 0.0, "b": 1.0},
        {"Z": 1, "a": 10.0, "b": 11.0},
        {"Z": 2, "a": 20.0, "b": 21.0},
    ]


def test_wide_form_of_nothing_is_empty_not_an_error():
    assert wide_form([], column="mean", axis_label="Z") == (["Z"], [])

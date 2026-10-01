"""Unit tests for the generic results-table plot builder."""

import numpy as np
import pytest

from imswitch.improcess.model.table_plots import (
    build_plot_payloads,
    column_values,
    numeric_columns,
)


def _records():
    return [
        {"area": 10.0, "ecc": 0.1, "label": "a", "flag": True},
        {"area": 20.0, "ecc": 0.2, "label": "b", "flag": False},
        {"area": 30.0, "ecc": 0.3, "label": "c", "flag": True},
        {"area": 40.0, "ecc": 0.5, "label": "d", "flag": False},
        {"area": "n/a", "ecc": 0.4, "label": "e", "flag": True},  # non-numeric area
    ]


def test_column_values_marks_non_numeric_and_bool_as_nan():
    values = column_values(_records(), "area")
    assert values[:4].tolist() == [10.0, 20.0, 30.0, 40.0]
    assert np.isnan(values[4])
    # Booleans must never count as numeric data.
    assert np.all(np.isnan(column_values(_records(), "flag")))


def test_numeric_columns_filters_text_columns():
    cols = ["area", "ecc", "label", "flag"]
    assert numeric_columns(cols, _records()) == ["area", "ecc"]


def test_histogram_payload_carries_finite_values_only():
    payloads = build_plot_payloads(["area"], _records(), {"kind": "histogram", "column": "area"})
    assert len(payloads) == 1
    series = payloads[0].series[0]
    assert series.kind == "histogram"
    assert series.y.tolist() == [10.0, 20.0, 30.0, 40.0]
    assert series.style["bins"] == 50


def test_line_payload_sorts_by_x():
    records = [{"x": 3.0, "y": 1.0}, {"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 3.0}]
    payloads = build_plot_payloads(
        ["x", "y"], records, {"kind": "line", "x_column": "x", "y_column": "y"}
    )
    series = payloads[0].series[0]
    assert series.kind == "line"
    assert series.x.tolist() == [1.0, 2.0, 3.0]
    assert series.y.tolist() == [2.0, 3.0, 1.0]


def test_scatter_payload_keeps_input_order_and_drops_incomplete_rows():
    records = [{"x": 3.0, "y": 1.0}, {"x": None, "y": 2.0}, {"x": 2.0, "y": 3.0}]
    payloads = build_plot_payloads(
        ["x", "y"], records, {"kind": "scatter", "x_column": "x", "y_column": "y"}
    )
    series = payloads[0].series[0]
    assert series.kind == "scatter"
    assert series.x.tolist() == [3.0, 2.0]
    assert series.y.tolist() == [1.0, 3.0]


def test_hist2d_payload_is_image_with_bin_edges():
    payloads = build_plot_payloads(
        ["area", "ecc"], _records(), {"kind": "hist2d", "x_column": "area", "y_column": "ecc", "bins": 4}
    )
    series = payloads[0].series[0]
    assert series.kind == "image"
    assert series.y.shape == (4, 4)
    assert series.style["x_edges"].size == 5
    assert series.style["y_edges"].size == 5
    assert series.y.sum() == 4  # four rows with both columns numeric


def test_pca_returns_two_component_scatter():
    rng = np.random.default_rng(0)
    records = [
        {"a": float(a), "b": float(a) * 2 + rng.normal(), "c": rng.normal()}
        for a in range(20)
    ]
    payloads = build_plot_payloads(
        ["a", "b", "c"], records, {"kind": "pca", "columns": ["a", "b", "c"]}
    )
    series = payloads[0].series[0]
    assert series.kind == "scatter"
    assert series.x.size == 20 and series.y.size == 20
    ratio = payloads[0].metadata["explained_variance_ratio"]
    assert len(ratio) == 2 and ratio[0] >= ratio[1]


def test_pca_is_deterministic_and_sign_pinned():
    # Feature 'a' dominates the variance, so PC1 should track it with a fixed
    # (positive-correlation) sign on every run — no SVD sign flips.
    records = [{"a": float(i), "b": float(-i) * 0.01, "c": 0.0} for i in range(15)]
    spec = {"kind": "pca", "columns": ["a", "b", "c"], "standardize": False}
    first = build_plot_payloads(["a", "b", "c"], records, spec)[0].series[0]
    second = build_plot_payloads(["a", "b", "c"], records, spec)[0].series[0]
    np.testing.assert_array_equal(first.x, second.x)
    a_values = np.arange(15, dtype=float)
    # PC1 correlates positively with the dominant feature under svd_flip.
    assert np.corrcoef(first.x, a_values)[0, 1] > 0


def test_pca_requires_two_numeric_columns():
    records = [{"a": 1.0, "txt": "x"}, {"a": 2.0, "txt": "y"}]
    with pytest.raises(ValueError, match="at least two numeric columns"):
        build_plot_payloads(["a", "txt"], records, {"kind": "pca", "columns": ["a", "txt"]})


def test_histogram_requires_numeric_column():
    with pytest.raises(ValueError, match="no numeric values"):
        build_plot_payloads(["label"], _records(), {"kind": "histogram", "column": "label"})


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="Unknown plot kind"):
        build_plot_payloads(["area"], _records(), {"kind": "violin"})


def test_empty_records_raises():
    with pytest.raises(ValueError, match="No rows"):
        build_plot_payloads(["area"], [], {"kind": "histogram", "column": "area"})


def test_umap_without_package_raises_runtime_error():
    pytest.importorskip  # keep import side effects explicit
    try:
        import umap  # noqa: F401
    except Exception:
        rng = np.random.default_rng(1)
        records = [{"a": rng.normal(), "b": rng.normal(), "c": rng.normal()} for _ in range(10)]
        with pytest.raises(RuntimeError, match="umap-learn"):
            build_plot_payloads(["a", "b", "c"], records, {"kind": "umap", "columns": ["a", "b", "c"]})


# --------------------------------------------------------------------------
# A-20 / P-4.8 — mixed units must not be plotted numerically
# --------------------------------------------------------------------------

def _mixed_rows():
    return [
        {"roi": "a", "area_cal": 2.0, "perimeter_cal": 4.0, "spatial_unit": "um"},
        {"roi": "b", "area_cal": 3.0, "perimeter_cal": 6.0, "spatial_unit": "nm"},
    ]


def test_mixed_length_units_are_converted_to_the_smallest():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = harmonize_units(_mixed_rows(), ["perimeter_cal"])
    assert [row["spatial_unit"] for row in rows] == ["nm", "nm"]
    # 4 um is 4000 nm; the nm row is untouched.
    assert rows[0]["perimeter_cal"] == pytest.approx(4000.0)
    assert rows[1]["perimeter_cal"] == pytest.approx(6.0)


def test_an_area_column_converts_by_the_square_of_the_ratio():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = harmonize_units(_mixed_rows(), ["area_cal"])
    assert rows[0]["area_cal"] == pytest.approx(2.0 * 1000**2)


def test_a_unit_that_cannot_be_converted_is_refused_by_name():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = [
        {"area_cal": 1.0, "spatial_unit": "um"},
        {"area_cal": 2.0, "spatial_unit": "arb"},
    ]
    with pytest.raises(ValueError, match="arb"):
        harmonize_units(rows, ["area_cal"])


def test_pixel_columns_are_never_converted():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = [
        {"area_px": 16, "spatial_unit": "um"},
        {"area_px": 16, "spatial_unit": "nm"},
    ]
    assert harmonize_units(rows, ["area_px"]) == rows


def test_one_unit_throughout_is_left_alone():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = [{"area_cal": 1.0, "spatial_unit": "um"}, {"area_cal": 2.0, "spatial_unit": "um"}]
    assert harmonize_units(rows, ["area_cal"]) is rows


def test_build_plot_payloads_applies_the_unit_rule():
    payloads = build_plot_payloads(
        ["roi", "perimeter_cal", "spatial_unit"],
        _mixed_rows(),
        {"kind": "histogram", "column": "perimeter_cal"},
    )
    assert sorted(payloads[0].series[0].y) == pytest.approx([6.0, 4000.0])


def test_pixels_and_micrometres_are_a_conflict_not_a_default():
    """px is a unit too, and it is not convertible to um."""
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = [
        {"area_cal": 16.0, "spatial_unit": "px"},
        {"area_cal": 4.0, "spatial_unit": "um"},
    ]
    with pytest.raises(ValueError, match="px"):
        harmonize_units(rows, ["area_cal"])


def test_a_table_measured_entirely_in_pixels_still_plots():
    from imswitch.improcess.model.table_plots import harmonize_units

    rows = [{"area_cal": 16.0, "spatial_unit": "px"}] * 2
    assert harmonize_units(rows, ["area_cal"]) is rows

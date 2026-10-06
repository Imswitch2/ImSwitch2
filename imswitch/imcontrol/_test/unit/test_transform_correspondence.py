import numpy as np
import pytest

from imswitch.imcontrol.model import transform_correspondence as module
from imswitch.imcontrol.model.ulenses_localizer import GridLocalizationResult


def _grid_result(*, n_cols=4, n_rows=5, angle_offset=0.0, stagger_axis="rows"):
    origin = np.array([3.0, 4.0])
    basis_x = np.array([1.0 + angle_offset, 20.0])
    basis_y = np.array([30.0, 1.0])
    logical = np.asarray(
        [(i, j) for j in range(n_rows) for i in range(n_cols)], dtype=np.int64
    )
    points = []
    for i, j in logical:
        point = origin + i * basis_x + j * basis_y
        if stagger_axis == "rows":
            point = point + (j % 2) * 0.5 * basis_x
        elif stagger_axis == "columns":
            point = point + (i % 2) * 0.5 * basis_y
        points.append(point)
    points = np.asarray(points)
    return GridLocalizationResult(
        origin_rc=origin,
        basis_x_rc=basis_x,
        basis_y_rc=basis_y,
        n_cols=n_cols,
        n_rows=n_rows,
        stagger_axis=stagger_axis,
        stagger_fraction=0.5 if stagger_axis else 0.0,
        grid_points_rc=points,
        logical_indices=logical,
        detected_points_rc=points,
        num_cols=120,
        num_rows=160,
        diagnostics={"fit_rms_px": 0.1},
    )


def test_ulenses_strategy_uses_grid_geometry_then_local_focus_refinement(monkeypatch):
    grid = _grid_result()
    localization_calls = []

    def fake_localize_grid(image, **kwargs):
        localization_calls.append(kwargs)
        return grid

    refinement_calls = []

    def fake_refine(image, localized, **kwargs):
        refinement_calls.append((localized, kwargs))
        offset = 0.0 if len(refinement_calls) == 1 else 1.0
        return localized.grid_points_rc + offset

    monkeypatch.setattr(module, "localize_grid", fake_localize_grid)
    monkeypatch.setattr(module, "refine_grid_foci", fake_refine)

    spec = module.ulenses_grid_strategy_spec(lambda: (12.0, 13.0))
    params = dict(spec.default_parameters)
    params.update({"grid_size_mode": "manual", "n_cols": 4, "n_rows": 5})
    result = spec.run(np.zeros((8, 8)), np.zeros((8, 8)), params)

    assert len(localization_calls) == 2
    assert all(call["xp_guess"] == 12.0 for call in localization_calls)
    assert all(call["yp_guess"] == 13.0 for call in localization_calls)
    assert all(call["n_cols"] == 4 for call in localization_calls)
    assert all(call["n_rows"] == 5 for call in localization_calls)
    assert len(refinement_calls) == 2
    assert result.pair_count == 20
    assert np.allclose(result.target_points - result.source_points, 1.0)
    assert result.metadata["grid_localization"]["moving"]["stagger_axis"] == "rows"


def test_ulenses_strategy_auto_size_does_not_supply_counts(monkeypatch):
    grid = _grid_result()
    calls = []

    def fake_localize_grid(image, **kwargs):
        calls.append(kwargs)
        return grid

    monkeypatch.setattr(module, "localize_grid", fake_localize_grid)
    monkeypatch.setattr(
        module,
        "refine_grid_foci",
        lambda _image, localized, **_kwargs: localized.grid_points_rc,
    )

    spec = module.ulenses_grid_strategy_spec()
    spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)

    assert len(calls) == 2
    assert all(call["n_cols"] is None for call in calls)
    assert all(call["n_rows"] is None for call in calls)


def test_ulenses_strategy_refuses_different_grid_shapes(monkeypatch):
    localizations = iter([_grid_result(n_cols=4), _grid_result(n_cols=5)])
    monkeypatch.setattr(
        module,
        "localize_grid",
        lambda image, **kwargs: next(localizations),
    )

    spec = module.ulenses_grid_strategy_spec()
    with pytest.raises(ValueError, match="same finite focus set"):
        spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)


def test_ulenses_strategy_refuses_different_stagger_axes(monkeypatch):
    localizations = iter([
        _grid_result(stagger_axis="rows"),
        _grid_result(stagger_axis="columns"),
    ])
    monkeypatch.setattr(
        module,
        "localize_grid",
        lambda image, **kwargs: next(localizations),
    )

    spec = module.ulenses_grid_strategy_spec()
    with pytest.raises(ValueError, match="stagger axis"):
        spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)


def test_ulenses_strategy_exposes_typed_grid_and_focus_parameters():
    spec = module.ulenses_grid_strategy_spec()

    assert spec.parameter_specs["grid_size_mode"].choices == ("auto", "manual")
    assert spec.parameter_specs["stagger_axis"].choices == (
        "auto", "rows", "columns"
    )
    assert "grid_threshold_rel" in spec.parameter_specs
    assert "focus_refine_radius" in spec.parameter_specs
    assert "residual_threshold" not in spec.default_parameters
    assert spec.default_parameters["use_ulenses_widget_guesses"] is True


def test_ulenses_strategy_exposes_independent_localization_diagnostics(monkeypatch):
    grid = _grid_result()
    monkeypatch.setattr(module, "localize_grid", lambda image, **kwargs: grid)
    monkeypatch.setattr(
        module, "refine_grid_foci",
        lambda _image, localized, **_kwargs: localized.grid_points_rc + 0.25,
    )
    spec = module.ulenses_grid_strategy_spec()
    localized = spec.localize(np.zeros((16, 16)), spec.default_parameters)

    assert localized.metadata["n_rows"] == grid.n_rows
    assert localized.metadata["n_cols"] == grid.n_cols
    assert localized.diagnostics is not None
    np.testing.assert_allclose(localized.diagnostics.detected_points, grid.detected_points_rc)
    np.testing.assert_allclose(localized.diagnostics.predicted_points, grid.grid_points_rc)
    np.testing.assert_allclose(localized.diagnostics.refined_points, grid.grid_points_rc + 0.25)

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import LOCALIZATION_DEFAULTS
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
        matched_mask=np.ones(points.shape[0], dtype=bool),
        measured_points_rc=points,
        num_cols=120,
        num_rows=160,
        diagnostics={"fit_rms_px": 0.1},
    )


def test_ulenses_strategy_uses_shared_backend_measured_positions(monkeypatch):
    moving = _grid_result()
    reference = _grid_result()
    reference = GridLocalizationResult(
        **{
            **reference.__dict__,
            "measured_points_rc": reference.measured_points_rc + 1.0,
        }
    )
    localization_calls = []
    grids = iter([moving, reference])

    def fake_localize_grid(image, **kwargs):
        localization_calls.append(kwargs)
        return next(grids)

    monkeypatch.setattr(module, "localize_grid", fake_localize_grid)

    spec = module.ulenses_grid_strategy_spec(lambda: (12.0, 13.0))
    params = dict(spec.default_parameters)
    params.update({"grid_size_mode": "manual", "n_cols": 4, "n_rows": 5})
    result = spec.run(np.zeros((8, 8)), np.zeros((8, 8)), params)

    assert len(localization_calls) == 2
    assert all(call["xp_guess"] == 12.0 for call in localization_calls)
    assert all(call["yp_guess"] == 13.0 for call in localization_calls)
    assert all(call["n_cols"] == 4 for call in localization_calls)
    assert all(call["n_rows"] == 5 for call in localization_calls)
    assert all(call["registration_mode"] == "auto" for call in localization_calls)
    assert all(
        set(LOCALIZATION_DEFAULTS).issubset(call)
        for call in localization_calls
    )
    assert all("threshold_rel" not in call for call in localization_calls)
    assert all("gaussian_sigma" not in call for call in localization_calls)
    assert all("min_distance_fraction" not in call for call in localization_calls)
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
    assert "spot_threshold_rel" in spec.parameter_specs
    assert "spot_blur_sigma" in spec.parameter_specs
    assert "focus_search_window_px" in spec.parameter_specs
    assert spec.parameter_specs["registration_mode"].choices == (
        "auto", "finite_grid", "fft_lattice"
    )
    assert "residual_threshold" not in spec.default_parameters
    assert spec.default_parameters["use_ulenses_widget_guesses"] is True


def test_ulenses_strategy_exposes_independent_localization_diagnostics(monkeypatch):
    grid = _grid_result()
    monkeypatch.setattr(module, "localize_grid", lambda image, **kwargs: grid)
    spec = module.ulenses_grid_strategy_spec()
    localized = spec.localize(np.zeros((16, 16)), spec.default_parameters)

    assert localized.metadata["n_rows"] == grid.n_rows
    assert localized.metadata["n_cols"] == grid.n_cols
    assert localized.diagnostics is not None
    np.testing.assert_allclose(localized.diagnostics.detected_points, grid.detected_points_rc)
    np.testing.assert_allclose(localized.diagnostics.predicted_points, grid.grid_points_rc)
    np.testing.assert_allclose(localized.diagnostics.refined_points, grid.measured_points_rc)


def test_ulenses_strategy_matches_only_indices_measured_in_both_images(monkeypatch):
    source_grid = _grid_result(n_cols=4, n_rows=5)
    target_grid = _grid_result(n_cols=4, n_rows=5)
    source_mask = np.ones(20, dtype=bool)
    target_mask = np.ones(20, dtype=bool)
    source_mask[[2, 7]] = False
    target_mask[[7, 11]] = False
    source_grid = GridLocalizationResult(
        **{
            **source_grid.__dict__,
            "matched_mask": source_mask,
            "measured_points_rc": source_grid.grid_points_rc,
        }
    )
    target_grid = GridLocalizationResult(
        **{
            **target_grid.__dict__,
            "matched_mask": target_mask,
            "measured_points_rc": target_grid.grid_points_rc,
        }
    )
    localizations = iter([source_grid, target_grid])
    monkeypatch.setattr(module, "localize_grid", lambda _image, **_kwargs: next(localizations))
    spec = module.ulenses_grid_strategy_spec()
    result = spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)

    # Missing index 7 occurs on both sides; 2 and 11 occur on one side each.
    assert result.pair_count == 17
    assert result.metadata["measured_pair_count"] == 17
    assert result.metadata["moving_missing_count"] == 2
    assert result.metadata["reference_missing_count"] == 2


def test_ulenses_grid_auto_correspondence_resolves_flip_x(monkeypatch):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        GridOrdering, grid_ordering_permutation,
    )

    source_grid = _grid_result(n_cols=5, n_rows=4, stagger_axis=None)
    permutation = grid_ordering_permutation(
        source_grid.logical_indices, GridOrdering.FLIP_X
    )
    target_grid = _grid_result(n_cols=5, n_rows=4, stagger_axis=None)
    target_grid = GridLocalizationResult(
        **{
            **target_grid.__dict__,
            # Simulate an independently valid localization that chose the
            # opposite finite X identity convention in the second image.
            "measured_points_rc": source_grid.grid_points_rc[permutation]
            + np.array([2.0, -3.0]),
        }
    )
    localizations = iter([source_grid, target_grid])
    monkeypatch.setattr(
        module, "localize_grid", lambda _image, **_kwargs: next(localizations)
    )

    spec = module.ulenses_grid_strategy_spec()
    result = spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)

    assert result.metadata["correspondence"]["requested"] == "auto"
    assert result.metadata["correspondence"]["resolved"] == "flip_x"
    np.testing.assert_allclose(
        result.target_points - result.source_points, np.array([2.0, -3.0])
    )


def test_ulenses_grid_manual_correspondence_override_is_respected(monkeypatch):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        GridOrdering, grid_ordering_permutation,
    )

    source_grid = _grid_result(n_cols=5, n_rows=4, stagger_axis=None)
    permutation = grid_ordering_permutation(
        source_grid.logical_indices, GridOrdering.ROTATE_180
    )
    target_grid = _grid_result(n_cols=5, n_rows=4, stagger_axis=None)
    target_grid = GridLocalizationResult(
        **{
            **target_grid.__dict__,
            "measured_points_rc": source_grid.grid_points_rc[permutation]
            + np.array([1.0, 4.0]),
        }
    )
    localizations = iter([source_grid, target_grid])
    monkeypatch.setattr(
        module, "localize_grid", lambda _image, **_kwargs: next(localizations)
    )

    spec = module.ulenses_grid_strategy_spec()
    pairing = {"correspondence": "rotate_180"}
    result = spec.run(
        np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters,
        pairing_parameters=pairing,
    )

    assert result.metadata["correspondence"]["requested"] == "rotate_180"
    assert result.metadata["correspondence"]["resolved"] == "rotate_180"
    np.testing.assert_allclose(
        result.target_points - result.source_points, np.array([1.0, 4.0])
    )


def test_ulenses_grid_correspondence_parameter_is_separate_from_localization():
    spec = module.ulenses_grid_strategy_spec()

    assert "correspondence" not in spec.parameter_specs
    assert spec.pairing_parameter_specs["correspondence"].choices == (
        "auto", "canonical", "flip_x", "flip_y", "rotate_180",
        "transpose", "rotate_90_cw", "rotate_90_ccw", "anti_transpose",
    )
    assert spec.default_pairing_parameters["correspondence"] == "auto"

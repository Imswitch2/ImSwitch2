import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.ulenses_localizer import (
    GridLocalizationResult,
    LocalizationResult,
    localize_grid,
    localizer,
    refine_grid_foci,
)

ulenses_controller_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.ULensesController'
)


def _render_grid(
    *,
    shape=(180, 220),
    n_cols=6,
    n_rows=5,
    origin=(42.0, 38.0),
    basis_x=(4.0, 24.0),
    basis_y=(22.0, 3.0),
    stagger_axis=None,
    stagger_fraction=0.0,
    sigma=1.2,
):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    image = np.zeros(shape, dtype=np.float64)
    points = []
    origin = np.asarray(origin, dtype=float)
    basis_x = np.asarray(basis_x, dtype=float)
    basis_y = np.asarray(basis_y, dtype=float)
    for j in range(n_rows):
        for i in range(n_cols):
            point = origin + i * basis_x + j * basis_y
            if stagger_axis == "rows":
                point = point + (j % 2) * stagger_fraction * basis_x
            elif stagger_axis == "columns":
                point = point + (i % 2) * stagger_fraction * basis_y
            points.append(point)
            image += np.exp(
                -((yy - point[0]) ** 2 + (xx - point[1]) ** 2) / (2 * sigma**2)
            )
    return image, np.asarray(points)


def test_localizer_recovers_grid_periods_from_initial_guesses():
    height, width = 160, 192
    y, x = np.mgrid[0:height, 0:width]
    image = (
        np.cos(np.pi * (x - 2.3) / 8.0) ** 6
        * np.cos(np.pi * (y - 4.1) / 11.0) ** 6
    )

    result = localizer(image, xp_guess=8.0, yp_guess=11.0)

    assert result.xp == pytest.approx(8.0, abs=0.15)
    assert result.yp == pytest.approx(11.0, abs=0.15)
    assert result.xo == pytest.approx(2.3, abs=0.6)
    assert result.yo == pytest.approx(4.1, abs=0.6)


@pytest.mark.parametrize("stagger_axis", [None, "rows", "columns"])
def test_localize_grid_uses_manual_finite_shape_and_recovers_tilt_stagger(stagger_axis):
    fraction = 0.35 if stagger_axis else 0.0
    image, _ = _render_grid(
        stagger_axis=stagger_axis,
        stagger_fraction=fraction,
    )

    result = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
    )

    assert result.shape == (5, 6)
    assert result.diagnostics["grid_size_source"] == "manual"
    assert result.x_period_px == pytest.approx(np.hypot(4, 24), abs=0.5)
    assert result.y_period_px == pytest.approx(np.hypot(22, 3), abs=0.5)
    assert result.angle_deg == pytest.approx(np.degrees(np.arctan2(4, 24)), abs=1.0)
    assert result.stagger_axis == stagger_axis
    assert result.stagger_fraction == pytest.approx(fraction, abs=0.06)
    assert result.diagnostics["fit_rms_px"] < 0.5


def test_localize_grid_auto_infers_finite_shape():
    image, _ = _render_grid(stagger_axis="rows", stagger_fraction=0.4)

    result = localize_grid(image, xp_guess=24.3, yp_guess=22.2)

    assert result.shape == (5, 6)
    assert result.diagnostics["grid_size_source"] == "auto"
    assert result.stagger_axis == "rows"
    assert result.stagger_fraction == pytest.approx(0.4, abs=0.06)


def test_localize_grid_manual_counts_are_authoritative():
    image, _ = _render_grid()

    with pytest.raises(RuntimeError, match="manual finite topology"):
        localize_grid(
            image,
            xp_guess=24.3,
            yp_guess=22.2,
            n_cols=7,
            n_rows=5,
        )


def test_refine_grid_foci_preserves_grid_index_order():
    image, expected = _render_grid(stagger_axis="columns", stagger_fraction=0.3)
    grid = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
    )

    refined = refine_grid_foci(image, grid, search_radius_px=4)

    assert refined.shape == expected.shape
    assert np.max(np.linalg.norm(refined - expected, axis=1)) < 0.5


def test_controller_uses_current_periodicities_as_localizer_guesses(monkeypatch):
    calls = {}

    def fake_localizer(image, *, xp_guess, yp_guess):
        calls['image'] = image
        calls['xp_guess'] = xp_guess
        calls['yp_guess'] = yp_guess
        return LocalizationResult(
            xp=7.6,
            xo=1.25,
            yp=7.4,
            yo=2.5,
            nx_c=1,
            ny_c=1,
            num_cols=10,
            num_rows=10,
        )

    monkeypatch.setattr(ulenses_controller_module, 'localizer', fake_localizer)

    image = np.ones((10, 10))
    set_parameters = {}

    class Widget:
        def getParameters(self):
            return 0.0, 0.0, 157.5, 1182.0, 1200.0

        def setParameters(self, **kwargs):
            set_parameters.update(kwargs)

    fake_controller = SimpleNamespace(
        _widget=Widget(),
        _commChannel=SimpleNamespace(get_image=lambda: image),
        _logger=SimpleNamespace(warning=lambda _msg: None),
    )
    fake_controller.updateGrid = lambda: calls.__setitem__('updated', True)

    ulenses_controller_module.ULensesController.localize(fake_controller)

    assert calls['image'] is image
    assert calls['xp_guess'] == pytest.approx(1182.0 / 157.5)
    assert calls['yp_guess'] == pytest.approx(1200.0 / 157.5)
    assert set_parameters == {
        'x': 1.25,
        'y': 2.5,
        'px': 157.5,
        'upx': round(7.6 * 157.5, 2),
        'upy': round(7.4 * 157.5, 2),
    }
    assert calls['updated'] is True

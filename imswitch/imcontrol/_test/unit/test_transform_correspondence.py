import numpy as np
import pytest

from imswitch.imcontrol.model import transform_correspondence as module
from imswitch.imcontrol.model.ulenses_localizer import LocalizationResult


def test_ulenses_strategy_localizes_grid_then_uses_shared_matcher(monkeypatch):
    localizations = iter(
        [
            LocalizationResult(20, 1, 30, 2, 4, 5, 100, 120),
            LocalizationResult(22, 3, 28, 4, 4, 5, 100, 120),
        ]
    )
    guesses = []

    def fake_localizer(image, xp_guess, yp_guess):
        guesses.append((xp_guess, yp_guess))
        return next(localizations)

    matcher_call = {}

    def fake_matcher(source, target, **kwargs):
        matcher_call.update(kwargs)
        points = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        return points, points + 1.0

    monkeypatch.setattr(module, "localizer", fake_localizer)
    monkeypatch.setattr(module, "find_grid_correspondence", fake_matcher)

    spec = module.ulenses_grid_strategy_spec(lambda: (12.0, 13.0))
    result = spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)

    assert guesses == [(12.0, 13.0), (12.0, 13.0)]
    assert matcher_call["n_rows"] == 5
    assert matcher_call["n_cols"] == 4
    assert matcher_call["min_distance"] == 10
    assert result.metadata["localization"]["moving"]["x_period_px"] == 20.0
    assert result.metadata["localization"]["reference"]["y_period_px"] == 28.0


def test_ulenses_strategy_refuses_different_grid_shapes(monkeypatch):
    localizations = iter(
        [
            LocalizationResult(20, 1, 30, 2, 4, 5, 100, 120),
            LocalizationResult(20, 1, 30, 2, 5, 5, 100, 120),
        ]
    )
    monkeypatch.setattr(
        module,
        "localizer",
        lambda image, xp_guess, yp_guess: next(localizations),
    )

    spec = module.ulenses_grid_strategy_spec()
    with pytest.raises(ValueError, match="disagree"):
        spec.run(np.zeros((8, 8)), np.zeros((8, 8)), spec.default_parameters)

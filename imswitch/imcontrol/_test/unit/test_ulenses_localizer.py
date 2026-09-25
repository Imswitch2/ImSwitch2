import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.ulenses_localizer import LocalizationResult, localizer

ulenses_controller_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.ULensesController'
)


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

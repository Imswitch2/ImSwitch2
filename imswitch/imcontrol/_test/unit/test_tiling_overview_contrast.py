"""The live tiling overview's contrast.

Asked for on the rig (2026-11-18): every repaint fitted the levels to the
mosaic's own minimum and maximum, so a contrast could not be set, let alone
kept while tiles arrived.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.widgets.TilingWidget import TilingWidget  # noqa: E402


@pytest.fixture
def tiling(qtbot):
    widget = TilingWidget(None)
    qtbot.addWidget(widget)
    return widget


def _mosaic(rng, hot=True):
    image = np.clip(rng.normal(0.3, 0.03, (300, 400)), 0, 1).astype(np.float32)
    if hot:
        image[:3, :3] = 1.0  # a few saturated pixels
    return image


def _levels(widget):
    return tuple(float(v) for v in widget.overviewItem.getLevels())


def test_auto_contrast_is_on_and_ignores_a_few_hot_pixels(tiling):
    assert tiling.autoContrastCheck.isChecked()
    tiling.updateOverview(_mosaic(np.random.default_rng(0)))
    low, high = _levels(tiling)
    assert 0.1 < low < 0.3 < high < 0.5


def test_the_empty_canvas_around_the_first_tiles_does_not_set_the_floor(tiling):
    image = np.zeros((300, 300), dtype=np.float32)
    image[:100, :100] = _mosaic(np.random.default_rng(1))[:100, :100]
    tiling.updateOverview(image)
    assert _levels(tiling)[0] > 0.1


def test_a_blank_overview_shows_the_full_range(tiling):
    tiling.updateOverview(np.zeros((64, 64), dtype=np.float32))
    assert _levels(tiling) == (0.0, 1.0)


def test_dragging_the_levels_turns_auto_off_and_they_hold(tiling):
    rng = np.random.default_rng(2)
    tiling.updateOverview(_mosaic(rng))
    tiling.overviewHist.setLevels(0.25, 0.35)  # what a drag does
    assert not tiling.autoContrastCheck.isChecked()
    tiling.updateOverview(_mosaic(rng) * 0.5)
    assert _levels(tiling) == pytest.approx((0.25, 0.35))


def test_updates_alone_never_turn_auto_off(tiling):
    rng = np.random.default_rng(3)
    for scale in (1.0, 0.5, 2.0):
        tiling.updateOverview(np.clip(_mosaic(rng) * scale, 0, 1))
    assert tiling.autoContrastCheck.isChecked()


def test_ticking_auto_again_fits_the_current_overview(tiling):
    image = _mosaic(np.random.default_rng(4))
    tiling.updateOverview(image)
    fitted = _levels(tiling)
    tiling.overviewHist.setLevels(0.0, 1.0)
    assert not tiling.autoContrastCheck.isChecked()
    tiling.autoContrastCheck.setChecked(True)
    assert _levels(tiling) == pytest.approx(fitted)


def test_a_huge_mosaic_is_fitted_from_a_subsample(tiling, monkeypatch):
    import sys

    # The package exports the class under the module's name.
    module = sys.modules[TilingWidget.__module__]

    seen = []
    real = module.bestLevels
    monkeypatch.setattr(module, 'bestLevels', lambda a: (seen.append(a.shape), real(a))[1])
    tiling.updateOverview(np.full((4000, 3000), 0.3, dtype=np.float32))
    assert seen and max(seen[0]) <= module._AUTO_CONTRAST_SAMPLES_PER_SIDE

"""Tests for MoNaLISA grid localization robustness fixes."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.localizer import (
    _estimate_period,
    localizer,
)
from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder
from imswitch.improcess.reconstructors.monalisa.live_session import MonalisaLiveSession
from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor


def _projection_signal(n: int, period: float, offset: float, sigma: float = 1.5):
    """1D projection of a focus grid: Gaussian peaks every ``period`` px."""
    x = np.arange(n, dtype=float)
    y = np.zeros(n)
    c = offset
    while c < n + 3 * sigma:
        y += np.exp(-((x - c) ** 2) / (2 * sigma**2))
        c += period
    return y - y.mean()


def _grid_image(shape, period: float, offset: tuple[float, float], sigma: float = 1.8):
    rows, cols = shape
    ys, xs = np.mgrid[0:rows, 0:cols].astype(float)
    img = np.zeros(shape)
    cy = offset[0]
    while cy < rows + 3 * sigma:
        cx = offset[1]
        while cx < cols + 3 * sigma:
            img += 100 * np.exp(
                -(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * sigma**2)
            )
            cx += period
        cy += period
    return img


class TestEstimatePeriod:
    @pytest.mark.parametrize("period", [3.0, 3.5, 4.5, 7.0, 10.0, 11.05, 20.0])
    def test_recovers_period_across_range(self, period):
        """Periods below ~4 px used to be unfindable: the frequency axis was
        built with ``fftfreq(n_bins)`` instead of the transform length, which
        labeled the upper half of the positive-frequency bins negative."""
        sig = _projection_signal(400, period, offset=2.3)
        est = _estimate_period(period, sig)
        assert est == pytest.approx(period, abs=0.05)

    def test_no_peak_near_guess_raises_value_error(self):
        """Data without spectral content near the guess must fail loudly (it
        used to crash with IndexError deep inside the peak bookkeeping)."""
        with pytest.raises(ValueError, match="period guess"):
            _estimate_period(10.0, np.zeros(400))


class TestLocalizerGuessPlumbing:
    def test_localizer_finds_coarse_period_with_matching_guess(self):
        img = _grid_image((220, 220), 14.0, offset=(4.2, 3.7))
        loc = localizer(img, xp_guess=14.0, yp_guess=14.0)
        assert loc.xp == pytest.approx(14.0, abs=0.3)
        assert loc.yp == pytest.approx(14.0, abs=0.3)

    def test_pattern_finder_forwards_guesses(self):
        img = _grid_image((220, 220), 14.0, offset=(4.2, 3.7))
        row_offset, col_offset, row_period, col_period = PatternFinder().find(
            img, xp_guess=14.0, yp_guess=14.0
        )
        assert row_period == pytest.approx(14.0, abs=0.3)
        assert col_period == pytest.approx(14.0, abs=0.3)

    def test_pattern_finder_ignores_invalid_guesses(self):
        img = _grid_image((200, 200), 10.0, offset=(4.2, 3.7))
        row_offset, col_offset, row_period, col_period = PatternFinder().find(
            img, xp_guess="not-a-number", yp_guess=-3
        )
        assert row_period == pytest.approx(10.0, abs=0.3)
        assert col_period == pytest.approx(10.0, abs=0.3)

    def test_live_session_localization_uses_widget_periods_as_guesses(self):
        """The live path auto-localizes; the widget's period values must seed
        the search window, or any pattern coarser than ~12.5 px silently
        mislocalizes against the hardcoded 10 px default."""
        img = _grid_image((220, 220), 14.0, offset=(4.2, 3.7))
        data = img[np.newaxis, ...]

        loc = MonalisaLiveSession._resolve_localization(
            data, {"row_period": 14.0, "col_period": 14.0}
        )
        assert loc.xp == pytest.approx(14.0, abs=0.3)
        assert loc.yp == pytest.approx(14.0, abs=0.3)

    def test_live_session_localization_tolerates_missing_periods(self):
        img = _grid_image((200, 200), 10.0, offset=(4.2, 3.7))
        data = img[np.newaxis, ...]
        loc = MonalisaLiveSession._resolve_localization(data, {})
        assert loc.xp == pytest.approx(10.0, abs=0.3)


class TestBleachingCorrection:
    def test_offline_correction_is_linear_in_energy(self):
        """Frame energy compensation is ``E_0 / E_i`` (linear). The legacy 4th
        power overcorrected by the cube of the energy loss; the live session
        already used the linear ratio, so the two paths disagreed."""
        rec = MonalisaReconstructor()
        data = np.stack(
            [
                np.full((4, 4), 100.0),
                np.full((4, 4), 50.0),  # half the energy -> scale by exactly 2
            ]
        )
        corrected = rec._apply_bleaching_correction(data)
        np.testing.assert_allclose(corrected[0], 100.0)
        np.testing.assert_allclose(corrected[1], 100.0)

    def test_offline_correction_preserves_dtype_and_skips_empty_frames(self):
        rec = MonalisaReconstructor()
        data = np.stack(
            [
                np.full((4, 4), 100, dtype=np.uint16),
                np.zeros((4, 4), dtype=np.uint16),
            ]
        )
        corrected = rec._apply_bleaching_correction(data)
        # The SignalExtractor DLL reads the raw buffer; dtype must not change.
        assert corrected.dtype == np.uint16
        np.testing.assert_array_equal(corrected[1], 0)

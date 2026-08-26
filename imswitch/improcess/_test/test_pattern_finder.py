from types import SimpleNamespace

import numpy as np
import pytest
from scipy.signal import find_peaks

from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder


def _render_foci(shape, lattice, sigma=1.8, amp=100.0):
    rows, cols = shape
    ys, xs = np.mgrid[0:rows, 0:cols].astype(float)
    img = np.zeros(shape)
    fx, fy = lattice.points_in_frame(rows, cols, margin=3 * sigma)
    for cx, cy in zip(fx, fy):
        img += amp * np.exp(-(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * sigma**2))
    return img


class TestFindPatternOrLattice:
    def test_rectangular_grid_returns_pattern(self):
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        image = _render_foci((220, 220), truth)
        pattern, lattice = PatternFinder().findPatternOrLattice(
            image, xp_guess=11.0, yp_guess=11.0
        )
        assert pattern is not None
        row_offset, col_offset, row_period, col_period = pattern
        assert row_period == pytest.approx(11.05, abs=0.1)
        assert col_period == pytest.approx(11.05, abs=0.1)
        assert lattice is not None

    def test_diamond_returns_lattice_instead_of_raising(self):
        """The Find pattern regression: a 45-degree rotated square must come
        back as a lattice object, not crash the UI with a ValueError."""
        half = 12.0 / np.sqrt(2)
        truth = Lattice(a1=(half, half), a2=(-half, half), offset=(3.3, 5.1))
        image = _render_foci((220, 220), truth)
        pattern, lattice = PatternFinder().findPatternOrLattice(
            image, xp_guess=11.1, yp_guess=11.1
        )
        assert pattern is None
        assert lattice is not None
        assert lattice.axis_tilt_deg() == pytest.approx(45.0, abs=0.5)
        assert lattice.nearest_spacing() == pytest.approx(12.0, abs=0.15)

    def test_featureless_data_raises_value_error(self):
        """Flat data has no spectral peak anywhere: detection and the 1D
        refinement both fail, and the error must propagate (readably) rather
        than fabricating a lattice. (Pure noise is different: the legacy 1D
        fallback has always returned a best-effort pattern for it.)"""
        with pytest.raises(ValueError):
            PatternFinder().findPatternOrLattice(np.zeros((128, 128)))


class TestPatternFinder:
    """Tests for PatternFinder class, focusing on FFT peak selection logic."""

    def test_findPattern_delegates_to_robust_localizer(self, monkeypatch):
        """The legacy UI adapter should use the robust localizer internally."""
        image = np.zeros((8, 9), dtype=np.float32)

        def fake_localizer(input_image, **kwargs):
            np.testing.assert_array_equal(input_image, image)
            return SimpleNamespace(yo=1.0, xo=2.0, yp=3.0, xp=4.0)

        monkeypatch.setattr(
            'imswitch.improcess.reconstructors.monalisa.pattern_finder.robust_localize',
            fake_localizer,
        )

        assert PatternFinder().findPattern(image) == [1.0, 2.0, 3.0, 4.0]
        assert PatternFinder().find(image) == [1.0, 2.0, 3.0, 4.0]

    def test_findBestPeak_similar_heights_returns_leftmost(self):
        """
        When two peaks have similar prominence AND similar height,
        findBestPeak should return the leftmost (minimum index) peak.
        """
        pf = PatternFinder()
        
        # Create synthetic peak data with two peaks of similar prominence and height
        # Peak indices: [5, 10]
        # Prominences: [8.0, 7.5] - within 20% relative difference
        # Heights: [10.0, 9.5] - within 20% relative difference
        peaks = (
            np.array([5, 10]),  # peak positions
            {
                'prominences': np.array([8.0, 7.5]),
                'peak_heights': np.array([10.0, 9.5])
            }
        )
        
        # Verify prominences are similar: abs((8.0-7.5)/(8.0+7.5)) = 0.032 < 0.2 ✓
        # Verify heights are similar: abs((10.0-9.5)/(10.0+9.5)) = 0.026 < 0.2 ✓
        
        best_peak_idx = pf.findBestPeak(peaks)
        
        # Should return index 0 (the leftmost peak at position 5)
        assert best_peak_idx == 0
        
    def test_findBestPeak_similar_prominence_different_heights_returns_highest(self):
        """
        When two peaks have similar prominence but different heights,
        findBestPeak should return the peak with greater height.
        """
        pf = PatternFinder()
        
        # Prominences similar, heights different
        # Peak indices: [5, 10]
        # Prominences: [8.0, 7.5] - within 20%
        # Heights: [10.0, 5.0] - NOT within 20%
        peaks = (
            np.array([5, 10]),
            {
                'prominences': np.array([8.0, 7.5]),
                'peak_heights': np.array([10.0, 5.0])
            }
        )
        
        # Verify prominences similar: abs((8.0-7.5)/(8.0+7.5)) = 0.032 < 0.2 ✓
        # Verify heights different: abs((10.0-5.0)/(10.0+5.0)) = 0.333 > 0.2 ✓
        
        best_peak_idx = pf.findBestPeak(peaks)
        
        # Should return index 0 (peak with height 10.0)
        assert best_peak_idx == 0
        
    def test_findBestPeak_different_prominence_returns_most_prominent(self):
        """
        When two peaks have different prominence,
        findBestPeak should return the most prominent peak regardless of height.
        """
        pf = PatternFinder()
        
        # Prominences different, heights can be anything
        # Peak indices: [5, 10]
        # Prominences: [10.0, 5.0] - NOT within 20%
        peaks = (
            np.array([5, 10]),
            {
                'prominences': np.array([10.0, 5.0]),
                'peak_heights': np.array([8.0, 12.0])  # second is higher but less prominent
            }
        )
        
        # Verify prominences different: abs((10.0-5.0)/(10.0+5.0)) = 0.333 > 0.2 ✓
        
        best_peak_idx = pf.findBestPeak(peaks)
        
        # Should return index 0 (most prominent peak)
        assert best_peak_idx == 0
        
    def test_findBestPeak_relative_difference_formula_not_constant(self):
        """
        Regression test: verify the relative difference formula produces
        non-constant values (not always 1.0 as the bug would cause).
        """
        pf = PatternFinder()
        
        # Test case 1: identical heights -> relative diff = 0
        peaks1 = (
            np.array([5, 10]),
            {
                'prominences': np.array([8.0, 7.5]),
                'peak_heights': np.array([10.0, 10.0])  # identical
            }
        )
        
        # Test case 2: very different heights -> relative diff close to 1
        peaks2 = (
            np.array([5, 10]),
            {
                'prominences': np.array([8.0, 7.5]),
                'peak_heights': np.array([10.0, 0.1])  # very different
            }
        )
        
        # The bug would have made both cases return the same result
        # With the fix, different height ratios should lead to different outcomes
        
        result1 = pf.findBestPeak(peaks1)
        result2 = pf.findBestPeak(peaks2)
        
        # Case 1: identical heights -> should pick leftmost (index 0)
        assert result1 == 0
        
        # Case 2: very different heights -> should pick highest (index 0)
        assert result2 == 0
        
        # Create a case where second peak is higher to verify proper selection
        peaks3 = (
            np.array([5, 10]),
            {
                'prominences': np.array([8.0, 7.5]),
                'peak_heights': np.array([1.0, 10.0])  # second is much higher
            }
        )
        result3 = pf.findBestPeak(peaks3)
        assert result3 == 1  # should pick index 1 (higher peak)

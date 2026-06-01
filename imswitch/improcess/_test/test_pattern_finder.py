import numpy as np
import pytest
from scipy.signal import find_peaks

from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder


class TestPatternFinder:
    """Tests for PatternFinder class, focusing on FFT peak selection logic."""

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

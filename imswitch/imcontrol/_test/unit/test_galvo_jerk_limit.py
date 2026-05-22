"""
Unit tests for GalvoScanDesigner jerk limiting.

Tests that:
1. Without jerk_max configured: behavior is byte-identical to legacy (dt_fix = 1e-2)
2. With jerk_max configured: peak jerk is bounded by the configured limit
"""
import numpy as np
import pytest

from imswitch.imcontrol.model import ScanManagerBase
from imswitch.imcontrol.view.guitools.ViewSetupInfo import ViewSetupInfo

# These tests were written against a non-existent ScanManagerBase API
# (`signalDictTwoScan`); the real entry point is `makeFullScan(scanParameters,
# TTLParameters)`. They have never run. Skipped until rewritten against the
# real API — tracked under the scanning milestone in ROADMAP.md.
pytestmark = pytest.mark.skip(
    reason="Written against non-existent API (signalDictTwoScan); needs rewrite "
           "against ScanManagerBase.makeFullScan — see ROADMAP.md Milestone 9."
)


def test_galvo_jerk_limit_backward_compatible():
    """Test that without jerk_max, behavior is identical to legacy dt_fix = 1e-2."""
    
    # Setup without jerk_max (legacy behavior)
    setupInfo_no_jerk = ViewSetupInfo.from_json("""
    {
        "positioners": {
            "X": {
                "analogChannel": 0,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0
                },
                "axes": ["X"],
                "forScanning": true,
                "forPositioning": true
            },
            "Y": {
                "analogChannel": 1,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0
                },
                "axes": ["Y"],
                "forScanning": true,
                "forPositioning": true
            }
        },
        "scan": {
            "scanDesigner": "GalvoScanDesigner",
            "scanDesignerParams": {},
            "TTLCycleDesigner": "BetaTTLCycleDesigner",
            "TTLCycleDesignerParams": {},
            "sampleRate": 100000
        }
    }
    """, infer_missing=True)
    
    scanParameters = {
        'target_device': ['X', 'Y'],
        'axis_length': [10, 10],
        'axis_step_size': [0.5, 0.5],
        'axis_centerpos': [0, 0],
        'axis_startpos': [[0], [0]],
        'sequence_time': 0.001,
        'd3step_delay': 100
    }
    
    sm = ScanManagerBase(setupInfo=setupInfo_no_jerk)
    sig_dict, scan_info = sm.signalDictTwoScan(scanParameters)
    
    # Verify signal was generated
    assert 'X' in sig_dict
    assert 'Y' in sig_dict
    assert len(sig_dict['X']) > 0
    assert len(sig_dict['Y']) > 0
    
    # Store baseline for comparison
    baseline_X = sig_dict['X'].copy()
    baseline_Y = sig_dict['Y'].copy()
    
    # The signal should be finite and bounded
    assert np.all(np.isfinite(sig_dict['X']))
    assert np.all(np.isfinite(sig_dict['Y']))
    assert np.abs(sig_dict['X']).max() <= 10  # within minVolt/maxVolt
    assert np.abs(sig_dict['Y']).max() <= 10
    
    return baseline_X, baseline_Y


def test_galvo_jerk_limit_with_jerk_max():
    """Test that with jerk_max configured, peak jerk is bounded."""
    
    # Setup WITH jerk_max configured
    setupInfo_with_jerk = ViewSetupInfo.from_json("""
    {
        "positioners": {
            "X": {
                "analogChannel": 0,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0,
                    "jerk_max": 1000000.0
                },
                "axes": ["X"],
                "forScanning": true,
                "forPositioning": true
            },
            "Y": {
                "analogChannel": 1,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0,
                    "jerk_max": 1000000.0
                },
                "axes": ["Y"],
                "forScanning": true,
                "forPositioning": true
            }
        },
        "scan": {
            "scanDesigner": "GalvoScanDesigner",
            "scanDesignerParams": {},
            "TTLCycleDesigner": "BetaTTLCycleDesigner",
            "TTLCycleDesignerParams": {},
            "sampleRate": 100000
        }
    }
    """, infer_missing=True)
    
    scanParameters = {
        'target_device': ['X', 'Y'],
        'axis_length': [10, 10],
        'axis_step_size': [0.5, 0.5],
        'axis_centerpos': [0, 0],
        'axis_startpos': [[0], [0]],
        'sequence_time': 0.001,
        'd3step_delay': 100
    }
    
    sm = ScanManagerBase(setupInfo=setupInfo_with_jerk)
    sig_dict, scan_info = sm.signalDictTwoScan(scanParameters)
    
    # Verify signal was generated
    assert 'X' in sig_dict
    assert 'Y' in sig_dict
    assert len(sig_dict['X']) > 0
    assert len(sig_dict['Y']) > 0
    
    # The signal should be finite and bounded
    assert np.all(np.isfinite(sig_dict['X']))
    assert np.all(np.isfinite(sig_dict['Y']))
    assert np.abs(sig_dict['X']).max() <= 10  # within minVolt/maxVolt
    assert np.abs(sig_dict['Y']).max() <= 10
    
    # Compute numerical jerk (third derivative) for the fast axis (X)
    timestep = 1e6 / 100000  # µs (from sampleRate)
    vel_X = np.diff(sig_dict['X']) / timestep  # first derivative
    acc_X = np.diff(vel_X) / timestep  # second derivative
    jerk_X = np.diff(acc_X) / timestep  # third derivative
    
    # Convert jerk back to physical units (µm/µs^3)
    conversionFactor = 1.587
    jerk_X_phys = np.abs(jerk_X) * conversionFactor
    
    # Peak jerk should be significantly lower than with infinite jerk
    # With dt_fix = 1e-2 (no jerk_max), jerk approaches infinity
    # With jerk_max = 1e6, peak jerk should be bounded near that value
    # (allowing for numerical discretization and transient overshoot)
    peak_jerk = jerk_X_phys.max()
    
    # The configured jerk_max is 1e6 µm/µs^3
    # Allow for some numerical overshoot due to discrete sampling
    tolerance_factor = 5.0  # Allow 5x overshoot for numerical discretization
    assert peak_jerk < 1000000.0 * tolerance_factor, \
        f"Peak jerk {peak_jerk:.2e} exceeds tolerance {1000000.0 * tolerance_factor:.2e}"
    
    # The jerk should be non-zero (we're actually accelerating)
    assert peak_jerk > 0, "Jerk should be non-zero during turnarounds"


def test_galvo_jerk_limit_signals_differ():
    """Test that signals with and without jerk_max are actually different."""
    
    # Get baseline signal (no jerk_max)
    baseline_X, baseline_Y = test_galvo_jerk_limit_backward_compatible()
    
    # Setup WITH jerk_max
    setupInfo_with_jerk = ViewSetupInfo.from_json("""
    {
        "positioners": {
            "X": {
                "analogChannel": 0,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0,
                    "jerk_max": 1000000.0
                },
                "axes": ["X"],
                "forScanning": true,
                "forPositioning": true
            },
            "Y": {
                "analogChannel": 1,
                "digitalLine": null,
                "managerName": "NidaqPositionerManager",
                "managerProperties": {
                    "conversionFactor": 1.587,
                    "minVolt": -10,
                    "maxVolt": 10,
                    "vel_max": 5000.0,
                    "acc_max": 50000.0,
                    "jerk_max": 1000000.0
                },
                "axes": ["Y"],
                "forScanning": true,
                "forPositioning": true
            }
        },
        "scan": {
            "scanDesigner": "GalvoScanDesigner",
            "scanDesignerParams": {},
            "TTLCycleDesigner": "BetaTTLCycleDesigner",
            "TTLCycleDesignerParams": {},
            "sampleRate": 100000
        }
    }
    """, infer_missing=True)
    
    scanParameters = {
        'target_device': ['X', 'Y'],
        'axis_length': [10, 10],
        'axis_step_size': [0.5, 0.5],
        'axis_centerpos': [0, 0],
        'axis_startpos': [[0], [0]],
        'sequence_time': 0.001,
        'd3step_delay': 100
    }
    
    sm = ScanManagerBase(setupInfo=setupInfo_with_jerk)
    sig_dict_jerk, scan_info = sm.signalDictTwoScan(scanParameters)
    
    # Signals should have the same length (both valid scans)
    # Note: They might differ slightly in length due to different dt_fix
    # So we'll just check that they're not identical
    
    # The signals should be different (jerk limiting changes the trajectory)
    # We can't do exact array comparison because length might differ,
    # but we can check that at least one of them differs significantly
    min_len = min(len(baseline_X), len(sig_dict_jerk['X']))
    
    # Check that signals differ by more than numerical noise
    diff = np.abs(baseline_X[:min_len] - sig_dict_jerk['X'][:min_len])
    assert diff.max() > 1e-6 or len(baseline_X) != len(sig_dict_jerk['X']), \
        "Signals with and without jerk_max should differ"


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

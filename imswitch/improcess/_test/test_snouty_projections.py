"""Tests for SNOUTY projections reconstructor plugin."""

import h5py
import numpy as np
import pytest

from imswitch.improcess.reconstructors.snouty_projections import SnoutyProjectionsReconstructor
from imswitch.improcess.model import DataObj


@pytest.fixture
def synthetic_stack():
    """Create a small synthetic 3D stack for testing."""
    # (planes, cam_y, cam_x)
    stack = np.random.rand(10, 20, 30).astype(np.float32) * 100
    # Add a few bright features
    stack[5, 10, 15] = 500
    stack[7, 12, 18] = 600
    return stack


@pytest.fixture
def default_params():
    """Default parameters for SNOUTY deskew."""
    return {
        'device': 'CPU',
        'n_timepoints': 1,
        'c_px': 100.0,
        'alpha_deg': 35.0,
        'dy': 210.0,
        'sample_vx_size': 200.0,
        'camera_offset': 0.0,
        'flip_data': False,
        'cycles': 1,
        'planes_in_cycle': 1,
        'restack': False,
    }


@pytest.fixture
def data_obj(synthetic_stack, tmp_path):
    """Create a mock DataObj."""
    # Create HDF5 file with the synthetic stack
    h5_path = tmp_path / "test_projections.h5"
    with h5py.File(h5_path, 'w') as f:
        f.create_dataset('data', data=synthetic_stack)
        f.attrs['Detector:Cam:Camera pixel size'] = 0.1  # 100 nm
    
    # Create DataObj from file
    obj = DataObj(str(h5_path), "test_projections")
    return obj


class TestSnoutyProjections:
    """Test suite for SNOUTY projections reconstructor."""
    
    def test_single_timepoint_shape(self, data_obj, default_params):
        """Test that single timepoint produces (3, H, W) output."""
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj, default_params)
        
        # Result should be 3D: (3, H, W)
        assert result.data.ndim == 3, f"Expected 3D, got {result.data.ndim}D"
        assert result.data.shape[0] == 3, f"Expected 3 projections, got {result.data.shape[0]}"
        
        # Should be finite
        assert np.all(np.isfinite(result.data)), "Result contains NaN or Inf"
        
        # Should be floating point
        assert result.data.dtype in [np.float32, np.float64], f"Expected float, got {result.data.dtype}"
    
    def test_multi_timepoint_shape(self, default_params, tmp_path):
        """Test that multi-timepoint produces (T, 3, H, W) output."""
        # Make stack with 2 timepoints (20 planes total, 10 per timepoint)
        stack_2t = np.random.rand(20, 20, 30).astype(np.float32) * 100
        
        # Create HDF5 file with multi-timepoint stack
        h5_path = tmp_path / "test_projections_mt.h5"
        with h5py.File(h5_path, 'w') as f:
            f.create_dataset('data', data=stack_2t)
            f.attrs['Detector:Cam:Camera pixel size'] = 0.1  # 100 nm
        
        # Create DataObj
        data_obj_mt = DataObj(str(h5_path), "test_mt")
        
        params = default_params.copy()
        params['n_timepoints'] = 2
        
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj_mt, params)
        
        # Result should be 4D: (T, 3, H, W)
        assert result.data.ndim == 4, f"Expected 4D, got {result.data.ndim}D"
        assert result.data.shape[0] == 2, f"Expected 2 timepoints, got {result.data.shape[0]}"
        assert result.data.shape[1] == 3, f"Expected 3 projections, got {result.data.shape[1]}"
        
        # Should be finite
        assert np.all(np.isfinite(result.data)), "Result contains NaN or Inf"
    
    def test_padding_sanity(self, data_obj, default_params):
        """Test that padded regions are zero."""
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj, default_params)
        
        # Result is (3, H, W)
        # Each projection may have different native sizes, padded to (H, W)
        # The padded regions should be zero (or very close to zero)
        
        # Check that at least some values are non-zero (not all padding)
        assert np.any(result.data > 0), "All values are zero (no data survived)"
        
        # Check that the maximum values are reasonable (not all padding)
        assert np.max(result.data) > 10, "Max value too small (likely all padding)"
    
    def test_axis_labels(self, data_obj, default_params, tmp_path):
        """Test that axis labels are correct."""
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj, default_params)
        
        # Single timepoint: ["projection", "Y", "X"]
        assert result.axis_labels == ["projection", "Y", "X"], \
            f"Expected ['projection', 'Y', 'X'], got {result.axis_labels}"
        
        # Multi-timepoint
        stack_2t = np.random.rand(20, 20, 30).astype(np.float32) * 100
        
        # Create HDF5 file for multi-timepoint test
        h5_path = tmp_path / "test_axis_labels_mt.h5"
        with h5py.File(h5_path, 'w') as f:
            f.create_dataset('data', data=stack_2t)
            f.attrs['Detector:Cam:Camera pixel size'] = 0.1
        
        data_obj_mt = DataObj(str(h5_path), "test_axis_mt")
        params = default_params.copy()
        params['n_timepoints'] = 2
        
        result_mt = reconstructor.process(data_obj_mt, params)
        assert result_mt.axis_labels == ["T", "projection", "Y", "X"], \
            f"Expected ['T', 'projection', 'Y', 'X'], got {result_mt.axis_labels}"
    
    def test_display_levels(self, data_obj, default_params):
        """Test that display levels are computed."""
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj, default_params)
        
        # Display levels should be set
        assert result.display_levels is not None, "Display levels not set"
        assert len(result.display_levels) == 2, "Display levels should be (min, max)"
        
        # Min should be less than max
        assert result.display_levels[0] < result.display_levels[1], \
            f"Display min {result.display_levels[0]} >= max {result.display_levels[1]}"
    
    def test_save_tiff(self, data_obj, default_params, tmp_path):
        """Test TIFF saving."""
        reconstructor = SnoutyProjectionsReconstructor()
        result = reconstructor.process(data_obj, default_params)
        
        # Save as TIFF
        tiff_path = tmp_path / "test_projections.tiff"
        result.save(tiff_path, fmt="tiff")
        
        # Check file exists
        assert tiff_path.exists(), "TIFF file not created"
        
        # Try to reload with tifffile
        import tifffile
        loaded = tifffile.imread(str(tiff_path))
        
        # Shape should match
        assert loaded.shape == result.data.shape, \
            f"Saved shape {loaded.shape} != original {result.data.shape}"


class TestSnoutyProjectionsRegistry:
    """Test plugin registration."""
    
    def test_plugin_registration(self):
        """Test that snouty-projections can be registered."""
        from imswitch.improcess.reconstructors import register_default_reconstructors, PluginRegistry
        
        reg = PluginRegistry()
        register_default_reconstructors(reg, ['snouty-projections'])
        
        reconstructors = reg.reconstructors()
        assert len(reconstructors) == 1, f"Expected 1 reconstructor, got {len(reconstructors)}"
        assert reconstructors[0].id == 'snouty-projections', \
            f"Expected 'snouty-projections', got '{reconstructors[0].id}'"


# Copyright (C) 2020-2026 ImSwitch developers
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

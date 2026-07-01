"""Tests for SNOUTY deskew reconstructor plugin."""

import os
import tempfile
from pathlib import Path

import h5py
import numpy as np
import pytest
import tifffile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor
from imswitch.improcess.reconstructors.snouty.metadata import snouty_params_from_attrs, DEFAULT_PARAMS
from imswitch.improcess.reconstructors.snouty.restack import restack_interleaved


class TestSnoutyMetadata:
    """Test metadata extraction from HDF5 attributes."""
    
    def test_empty_attrs_returns_defaults(self):
        """Empty attrs dict should return full defaults."""
        params = snouty_params_from_attrs({})
        assert params == DEFAULT_PARAMS
    
    def test_camera_pixel_size_conversion(self):
        """Camera pixel size should convert µm → nm (×1000)."""
        attrs = {"Detector:Cam:Camera pixel size": 0.108}
        params = snouty_params_from_attrs(attrs)
        assert params['c_px'] == 108.0
    
    def test_pixel_size_alternate_key(self):
        """Should also accept 'Pixel size' key variant."""
        attrs = {"Detector:Camera1:Pixel size": 0.1}
        params = snouty_params_from_attrs(attrs)
        assert params['c_px'] == 100.0
    
    def test_scan_step_conversion(self):
        """Scan step should convert µm → nm (×1000)."""
        attrs = {"MS-RESOLFT_Scan:cycleStepSizeUm": 0.21}
        params = snouty_params_from_attrs(attrs)
        assert params['dy'] == 210.0
    
    def test_ms_resolft_cycles(self):
        """Should extract cycles and planes_in_cycle."""
        attrs = {
            "MS-RESOLFT_Scan:cycleSteps": 10,
            "MS-RESOLFT_Scan:roSteps": 5
        }
        params = snouty_params_from_attrs(attrs)
        assert params['cycles'] == 10
        assert params['planes_in_cycle'] == 5
    
    def test_flip_data(self):
        """Should extract flip_data from positive_direction."""
        attrs = {"ScanStage:positive_direction": True}
        params = snouty_params_from_attrs(attrs)
        assert params['flip_data'] is True
    
    def test_partial_attrs(self):
        """Partial attrs should override only specified values."""
        attrs = {
            "Detector:Cam:Camera pixel size": 0.1,
            "MS-RESOLFT_Scan:cycleSteps": 3
        }
        params = snouty_params_from_attrs(attrs)
        assert params['c_px'] == 100.0
        assert params['cycles'] == 3
        assert params['alpha_deg'] == DEFAULT_PARAMS['alpha_deg']  # default preserved


class TestRestackInterleaved:
    """Test MS-RESOLFT de-interlacing."""
    
    def test_no_restack_when_insufficient_planes(self):
        """Should return original stack if too few planes."""
        stack = np.arange(12).reshape(4, 3, 1).astype(np.float32)
        result = restack_interleaved(stack, cycles=3, planes_in_cycle=2)
        # Expected 6 planes, got 4 → no change
        assert np.array_equal(result, stack)
    
    def test_restack_2_cycles_2_planes(self):
        """De-interlace 2 cycles × 2 planes/cycle."""
        # Interleaved order: p0c0, p1c0, p0c1, p1c1
        stack = np.array([0, 1, 2, 3]).reshape(4, 1, 1).astype(np.float32)
        result = restack_interleaved(stack, cycles=2, planes_in_cycle=2)
        # Expected order: p0c0, p0c1, p1c0, p1c1
        expected = np.array([0, 2, 1, 3]).reshape(4, 1, 1).astype(np.float32)
        assert np.array_equal(result, expected)
    
    def test_restack_preserves_extra_planes(self):
        """Should only return restacked first cycles*planes planes (discards extras)."""
        stack = np.arange(10).reshape(10, 1, 1).astype(np.float32)
        result = restack_interleaved(stack, cycles=2, planes_in_cycle=2)
        # Only first 4 planes returned, restacked
        assert result.shape == (4, 1, 1)
        assert result[0, 0, 0] == 0
        assert result[1, 0, 0] == 2  # swapped
        assert result[2, 0, 0] == 1  # swapped
        assert result[3, 0, 0] == 3


@pytest.fixture
def synthetic_3d_stack():
    """Create small synthetic raw 3D stack for testing."""
    rng = np.random.RandomState(42)
    # (planes, cam_y, cam_x)
    signal = rng.rand(8, 16, 16).astype(np.float32) * 100.0
    # SNOUTY subtracts camera_offset before clipping, so raw test data must
    # include the detector baseline or the reconstructed signal is all zero.
    stack = signal + DEFAULT_PARAMS["camera_offset"] + 1.0
    return stack


@pytest.fixture
def data_obj_3d(synthetic_3d_stack, tmp_path):
    """Create DataObj with synthetic 3D HDF5 data."""
    h5_path = tmp_path / "test_snouty_3d.h5"
    with h5py.File(h5_path, 'w') as f:
        f.create_dataset('data', data=synthetic_3d_stack)
        f.attrs['Detector:Cam:Camera pixel size'] = 0.1  # 100 nm
    
    data_obj = DataObj(str(h5_path), "test_3d")
    return data_obj


@pytest.fixture(scope="module")
def qapp():
    """Ensure Qt widgets are constructed under an offscreen QApplication."""
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class TestSnoutyReconstructor:
    """Test SNOUTY reconstructor integration."""
    
    def test_reconstructor_attributes(self):
        """Test reconstructor class attributes."""
        reconstructor = SnoutyReconstructor()
        assert reconstructor.id == "snouty"
        assert reconstructor.name == "SNOUTY deskew"
        assert "hdf5" in reconstructor.file_extensions
        assert "tiff" in reconstructor.file_extensions

    def test_result_physical_axis_scales(self):
        """SNOUTY result exposes voxel size as micrometer axis scales."""
        from imswitch.improcess.reconstructors.snouty.result import SnoutyResult

        params = DEFAULT_PARAMS.copy()
        params["sample_vx_size"] = 250.0
        result = SnoutyResult(
            name="scaled",
            data=np.zeros((2, 3, 4), dtype=np.float32),
            params=params,
        )

        assert result.axis_scales == [0.25, 0.25, 0.25]
        assert result.scale_unit == "um"

    @pytest.mark.parametrize(
        "shape, expected_planes",
        [
            ((2, 3, 4), {"XY": ["Y", "X"], "XZ": ["Z", "X"], "YZ": ["Z", "Y"]}),
            ((2, 2, 3, 4), {"XY": ["Y", "X"], "XZ": ["Z", "X"], "YZ": ["Z", "Y"]}),
        ],
    )
    def test_result_view_modes_display_orthogonal_planes(self, shape, expected_planes):
        """Each view mode must move the named axes into the displayed (last
        two) positions — the viewer slices along the leading axes, so a mode
        that only swaps the displayed axes would show a transposed XY slice
        instead of a true orthogonal section (regression for the XZ view)."""
        from imswitch.improcess.reconstructors.snouty.result import SnoutyResult

        result = SnoutyResult(
            name="planes",
            data=np.zeros(shape, dtype=np.float32),
            params=DEFAULT_PARAMS.copy(),
        )

        modes = {mode.name: mode for mode in result.view_modes}
        assert set(modes) == set(expected_planes)
        for name, mode in modes.items():
            assert sorted(mode.transpose) == list(range(len(shape)))
            transposed_labels = [result.axis_labels[i] for i in mode.transpose]
            assert transposed_labels[-2:] == expected_planes[name]

    def test_process_single_timepoint(self, data_obj_3d):
        """Test single-timepoint deskew."""
        reconstructor = SnoutyReconstructor()
        
        # Use defaults with single timepoint
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'CPU'
        params['n_timepoints'] = 1
        
        result = reconstructor.process(data_obj_3d, params)
        
        # Check result properties
        assert result.data.ndim == 3, "Single timepoint should be 3D (Z, Y, X)"
        assert result.data.dtype == np.float32 or result.data.dtype == np.float64
        assert result.axis_labels == ["Z", "Y", "X"]
        assert len(result.view_modes) == 3  # XY, XZ, YZ
        assert result.display_levels is not None
        
        # Check no NaNs
        assert not np.isnan(result.data).any()
        
        # Check reasonable intensity range
        assert np.max(result.data) > 0
        assert np.max(result.data) < 1000  # ~10x background-corrected signal
    
    def test_process_multi_timepoint(self, data_obj_3d):
        """Test multi-timepoint timelapse deskew."""
        reconstructor = SnoutyReconstructor()
        
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'CPU'
        params['n_timepoints'] = 2  # Split 8 planes into 2 timepoints
        
        result = reconstructor.process(data_obj_3d, params)
        
        # Check 4D shape
        assert result.data.ndim == 4, "Multi-timepoint should be 4D (T, Z, Y, X)"
        assert result.data.shape[0] == 2, "First axis should equal n_timepoints"
        assert result.axis_labels == ["T", "Z", "Y", "X"]
        assert len(result.view_modes) == 3  # XY, XZ, YZ (with T preserved)
        
        # Check each timepoint is valid
        for t in range(2):
            tp_data = result.data[t]
            assert not np.isnan(tp_data).any()
            assert np.max(tp_data) > 0
    
    def test_expected_output_shape(self, synthetic_3d_stack):
        """Test output shape matches geometry transformation."""
        # Manual computation of expected shape
        c_px = 100.0  # nm
        alpha_deg = 35.0
        dy = 210.0  # nm
        vx = 200.0  # nm
        
        alpha = np.deg2rad(alpha_deg)
        M = np.array([
            [c_px * np.sin(alpha), 0.0, 0.0],
            [c_px * np.cos(alpha), dy, 0.0],
            [0.0, 0.0, c_px],
        ]) / vx
        
        # Input shape after transpose: (cam_y=16, planes=8, cam_x=16)
        transposed_shape = np.array([16, 8, 16], dtype=np.float32)
        expected_out_shape = tuple(int(s) for s in np.ceil(M @ transposed_shape).astype(int))
        
        # Now run reconstruction
        reconstructor = SnoutyReconstructor()
        
        # Create temp HDF5
        with tempfile.NamedTemporaryFile(suffix='.h5', delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            with h5py.File(tmp_path, 'w') as f:
                f.create_dataset('data', data=synthetic_3d_stack)
            
            data_obj = DataObj(tmp_path, "test_shape")
            params = DEFAULT_PARAMS.copy()
            params['device'] = 'CPU'
            params['n_timepoints'] = 1
            
            result = reconstructor.process(data_obj, params)
            
            assert result.data.shape == expected_out_shape, \
                f"Expected {expected_out_shape}, got {result.data.shape}"
        finally:
            Path(tmp_path).unlink()
    
    def test_save_tiff(self, data_obj_3d, tmp_path):
        """Test saving result as TIFF."""
        reconstructor = SnoutyReconstructor()
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'CPU'
        params['n_timepoints'] = 1
        
        result = reconstructor.process(data_obj_3d, params)
        
        # Save as TIFF
        tiff_path = tmp_path / "result.tiff"
        result.save(tiff_path, fmt="tiff")
        
        # Reload and verify
        assert tiff_path.exists()
        reloaded = tifffile.imread(tiff_path)
        assert reloaded.shape == result.data.shape
        assert np.allclose(reloaded, result.data, rtol=1e-5)
    
    def test_save_hdf5_3d(self, data_obj_3d, tmp_path):
        """Test saving 3D result as HDF5."""
        reconstructor = SnoutyReconstructor()
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'CPU'
        params['n_timepoints'] = 1
        
        result = reconstructor.process(data_obj_3d, params)
        
        # Save as HDF5
        h5_path = tmp_path / "result.h5"
        result.save(h5_path, fmt="hdf5")
        
        # Reload and verify
        assert h5_path.exists()
        with h5py.File(h5_path, 'r') as f:
            assert 'volume' in f
            reloaded = f['volume'][:]
            assert reloaded.shape == result.data.shape
            assert np.allclose(reloaded, result.data, rtol=1e-5)
            
            # Check params saved as attributes
            assert 'c_px' in f.attrs
            assert f.attrs['c_px'] == params['c_px']
    
    def test_save_hdf5_4d(self, data_obj_3d, tmp_path):
        """Test saving 4D result as HDF5 with per-timepoint datasets."""
        reconstructor = SnoutyReconstructor()
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'CPU'
        params['n_timepoints'] = 2
        
        result = reconstructor.process(data_obj_3d, params)
        
        # Save as HDF5
        h5_path = tmp_path / "result_4d.h5"
        result.save(h5_path, fmt="hdf5")
        
        # Reload and verify
        assert h5_path.exists()
        with h5py.File(h5_path, 'r') as f:
            assert 't000' in f
            assert 't001' in f
            
            # Check each timepoint
            for t in range(2):
                ds_name = f"t{t:03d}"
                assert ds_name in f
                reloaded_tp = f[ds_name][:]
                assert reloaded_tp.shape == result.data[t].shape
                assert np.allclose(reloaded_tp, result.data[t], rtol=1e-5)


@pytest.mark.usefixtures("qapp")
class TestSnoutyParamsWidget:
    """Test SNOUTY parameter widget."""
    
    def test_get_values_defaults(self):
        """get_values() should return all parameters with defaults."""
        from imswitch.improcess.reconstructors.snouty.params_widget import SnoutyParamsWidget
        widget = SnoutyParamsWidget()
        
        values = widget.get_values()
        
        assert values['device'] == 'CPU'
        assert values['n_timepoints'] == 1
        assert values['c_px'] == DEFAULT_PARAMS['c_px']
        assert values['alpha_deg'] == DEFAULT_PARAMS['alpha_deg']
        assert values['dy'] == DEFAULT_PARAMS['dy']
        assert values['sample_vx_size'] == DEFAULT_PARAMS['sample_vx_size']
        assert values['camera_offset'] == DEFAULT_PARAMS['camera_offset']
        assert values['flip_data'] == DEFAULT_PARAMS['flip_data']
        assert values['cycles'] == DEFAULT_PARAMS['cycles']
        assert values['planes_in_cycle'] == DEFAULT_PARAMS['planes_in_cycle']
        assert values['restack'] == DEFAULT_PARAMS['restack']
    
    def test_set_from_attrs(self):
        """set_from_attrs() should update widget from metadata."""
        from imswitch.improcess.reconstructors.snouty.params_widget import SnoutyParamsWidget
        widget = SnoutyParamsWidget()
        
        attrs = {
            "Detector:Cam:Camera pixel size": 0.12,  # 120 nm
            "MS-RESOLFT_Scan:cycleSteps": 5,
            "ScanStage:positive_direction": True
        }
        
        widget.set_from_attrs(attrs)
        values = widget.get_values()
        
        assert values['c_px'] == 120.0
        assert values['cycles'] == 5
        assert values['flip_data'] is True
        # Other values should remain default
        assert values['alpha_deg'] == DEFAULT_PARAMS['alpha_deg']


class TestSnoutyGPU:
    """Test GPU deskew path (Phase D.2)."""
    
    def test_cupy_available_detection(self):
        """Test cupy_available() function."""
        from imswitch.improcess.reconstructors.snouty.deskew_gpu import cupy_available
        
        # Should return bool without raising
        result = cupy_available()
        assert isinstance(result, bool)
    
    def test_gpu_path_raises_clear_error_without_cupy(self, data_obj_3d):
        """When CuPy is missing, GPU path should raise a clear error."""
        import importlib.util
        
        # Check if cupy is actually available
        cupy_spec = importlib.util.find_spec("cupy")
        if cupy_spec is not None:
            pytest.skip("cupy is installed; cannot test missing-cupy error path")
        
        reconstructor = SnoutyReconstructor()
        params = DEFAULT_PARAMS.copy()
        params['device'] = 'GPU'
        params['n_timepoints'] = 1
        
        # Should raise RuntimeError mentioning cupy
        with pytest.raises(RuntimeError, match="(?i)cupy"):
            reconstructor.process(data_obj_3d, params)
    
    def test_gpu_path_runs_when_cupy_available(self, synthetic_3d_stack):
        """When CuPy is available, GPU path should produce valid output."""
        import importlib.util
        
        # Check if cupy is installed
        cupy_spec = importlib.util.find_spec("cupy")
        if cupy_spec is None:
            pytest.skip("cupy not installed")
        
        # Import now (after checking availability)
        from imswitch.improcess.reconstructors.snouty.deskew_gpu import cupy_available
        if not cupy_available():
            pytest.skip("cupy not available")
        
        # Create temp HDF5 file
        with tempfile.NamedTemporaryFile(suffix='.h5', delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            with h5py.File(tmp_path, 'w') as f:
                f.create_dataset('data', data=synthetic_3d_stack)
            
            data_obj = DataObj(tmp_path, "test_gpu")
            
            reconstructor = SnoutyReconstructor()
            params = DEFAULT_PARAMS.copy()
            params['device'] = 'GPU'
            params['n_timepoints'] = 1
            
            result = reconstructor.process(data_obj, params)
            
            # Check result is valid
            assert result.data.ndim == 3, "Should be 3D (Z, Y, X)"
            assert result.data.dtype in (np.float32, np.float64)
            assert not np.isnan(result.data).any()
            assert np.max(result.data) > 0
            
            # Should match expected shape from geometry
            c_px = 100.0
            alpha_deg = 35.0
            dy = 210.0
            vx = 200.0
            
            alpha = np.deg2rad(alpha_deg)
            M = np.array([
                [c_px * np.sin(alpha), 0.0, 0.0],
                [c_px * np.cos(alpha), dy, 0.0],
                [0.0, 0.0, c_px],
            ]) / vx
            
            transposed_shape = np.array([16, 8, 16], dtype=np.float32)
            expected_shape = tuple(int(s) for s in np.ceil(M @ transposed_shape).astype(int))
            
            assert result.data.shape == expected_shape, \
                f"Expected {expected_shape}, got {result.data.shape}"
        finally:
            Path(tmp_path).unlink()
    
    def test_gpu_cpu_parity(self, synthetic_3d_stack):
        """GPU and CPU paths should produce similar results."""
        import importlib.util
        
        cupy_spec = importlib.util.find_spec("cupy")
        if cupy_spec is None:
            pytest.skip("cupy not installed")
        
        from imswitch.improcess.reconstructors.snouty.deskew_gpu import cupy_available
        if not cupy_available():
            pytest.skip("cupy not available")
        
        # Create temp HDF5 file
        with tempfile.NamedTemporaryFile(suffix='.h5', delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            with h5py.File(tmp_path, 'w') as f:
                f.create_dataset('data', data=synthetic_3d_stack)
            
            data_obj = DataObj(tmp_path, "test_parity")
            
            reconstructor = SnoutyReconstructor()
            params = DEFAULT_PARAMS.copy()
            params['n_timepoints'] = 1
            
            # Run CPU
            params['device'] = 'CPU'
            result_cpu = reconstructor.process(data_obj, params)
            
            # Run GPU
            params['device'] = 'GPU'
            result_gpu = reconstructor.process(data_obj, params)
            
            # Shapes should match exactly
            assert result_cpu.data.shape == result_gpu.data.shape
            
            # Results should be close (small differences due to float precision)
            assert np.allclose(result_cpu.data, result_gpu.data, rtol=1e-4, atol=1e-4), \
                "CPU and GPU results differ beyond tolerance"
        finally:
            Path(tmp_path).unlink()


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

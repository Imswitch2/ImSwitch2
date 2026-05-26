"""Unit tests for TilingWorkflow."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import TilingParams, TilingWorkflow


class MockRecordingWorkflow:
    """Mock recording workflow for testing."""
    
    def __init__(self):
        self.run_calls = []
    
    def run(self, **kwargs):
        """Record the call for verification."""
        self.run_calls.append(kwargs)


def build_mock_facade():
    """Build a mock facade for testing."""
    facade = MagicMock()
    
    # Mock laser control
    facade.laser_con.laser_on = MagicMock()
    facade.laser_con.laser_off = MagicMock()
    facade.laser_con.set_constant_power = MagicMock()
    facade.laser_con.set_triggered_mode = MagicMock()
    facade.laser_con.set_modulation_mode = MagicMock()
    
    # Mock camera
    facade.cam.prepare_acquisition = MagicMock()
    facade.cam.start_acquisition = MagicMock()
    facade.cam.stop_acquisition = MagicMock()
    facade.cam.prepare_live = MagicMock()
    facade.cam.start_live = MagicMock()
    facade.cam.stop_live = MagicMock()
    facade.cam.wait_for_frame = MagicMock(return_value=True)
    
    # Return fake camera data (single frame)
    fake_frame = np.random.randint(0, 65535, (512, 512), dtype=np.uint16)
    facade.cam.get_data = MagicMock(return_value=np.array([fake_frame]))
    
    # Mock trigger
    facade.trig.connected = True
    facade.trig.snap_trigger = MagicMock()
    
    # Mock stage
    facade.stage_con.move_to = MagicMock()
    facade.stage_con.get_position = MagicMock(return_value=(10000.0, 20000.0))
    
    # Mock rotators
    facade.rotator_hwp.move_to_h = MagicMock()
    facade.rotator_hwp.move_to_v = MagicMock()
    facade.rotator_qwp.chained_move_to_h = MagicMock()
    facade.rotator_qwp.chained_move_to_v = MagicMock()
    
    return facade


class TestTilingWorkflow:
    """Test suite for TilingWorkflow."""
    
    def test_import(self):
        """Verify module imports."""
        from imswitch.imcontrol.model.workflows import TilingParams, TilingWorkflow
        assert TilingParams is not None
        assert TilingWorkflow is not None
    
    def test_params_initialization(self):
        """Test TilingParams dataclass initialization."""
        params = TilingParams(n_tiles=9)
        assert params.n_tiles == 9
        assert params.step_units == 1560
        assert params.laser_pin == 0
        assert params.camera_pin == 1
        assert params.pulsed is False
        assert params.laser_power_488_mw == 5.0
        assert params.exposure_us == 50000
        assert params.tile_display_size == 256
        assert params.save_individual is True
        assert params.skip_cell_targeting is False
    
    def test_workflow_initialization(self):
        """Test TilingWorkflow initialization."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        assert workflow.facade is facade
        assert workflow._recording is recording
        assert workflow.params is params
        assert workflow.seg_filter == {}
    
    def test_workflow_with_seg_filter(self):
        """Test TilingWorkflow with segmentation filter."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        seg_filter = {
            "area_um2_min": 100.0,
            "area_um2_max": 1000.0,
            "area_enabled": True,
        }
        
        workflow = TilingWorkflow(facade, recording, params, seg_filter)
        assert workflow.seg_filter == seg_filter
    
    def test_run_raises_without_save_folder(self):
        """Test run() raises ValueError if no save_folder provided."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        with pytest.raises(ValueError, match="save_folder must be provided"):
            workflow.run()
    
    def test_run_creates_tiles(self):
        """Test run() executes tiling scan."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=True,
                pulsed=False,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify stage moves (4 tiles + 1 move to origin at end)
            assert facade.stage_con.move_to.call_count == 5
            
            # Verify camera calls
            assert facade.cam.prepare_live.call_count == 4
            assert facade.cam.start_live.call_count == 4
            assert facade.cam.stop_live.call_count == 4
    
    def test_run_rounds_n_tiles_to_perfect_square(self):
        """Test run() rounds n_tiles up to perfect square."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # 5 tiles should round to 9 (3x3)
            params = TilingParams(
                n_tiles=5,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=False,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify 9 tiles were acquired (3x3 spiral) + 1 move to origin
            assert facade.stage_con.move_to.call_count == 10
    
    def test_run_hardware_triggered_mode(self):
        """Test run() uses hardware triggering when configured."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=True,
                laser_pin=2,
                camera_pin=3,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify triggered mode was set
            facade.laser_con.set_triggered_mode.assert_called_once()
            
            # Verify snap_trigger was called for each tile
            assert facade.trig.snap_trigger.call_count == 4
    
    def test_run_software_pulsed_mode(self):
        """Test run() uses software pulsing when no trigger available."""
        facade = build_mock_facade()
        facade.trig.connected = False
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=True,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify constant power mode was used
            facade.laser_con.set_constant_power.assert_called_once()
            
            # Verify laser on/off calls (4 tiles + 1 initial off after set_constant_power)
            assert facade.laser_con.laser_on.call_count == 4
            assert facade.laser_con.laser_off.call_count == 5
    
    def test_run_tile_callback(self):
        """Test run() calls tile_callback for each tile."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        callback_calls = []
        
        def tile_callback(image, gx, gy, idx, n_total):
            callback_calls.append((gx, gy, idx, n_total))
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run(tile_callback=tile_callback)
            
            # Verify callback was called for each tile
            assert len(callback_calls) == 4
            
            # Check that n_total is correct in all calls
            assert all(call[3] == 4 for call in callback_calls)
    
    def test_run_saves_individual_tiles(self):
        """Test run() saves individual .npy files when configured."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=True,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify .npy files were created
            npy_files = list(Path(tmpdir).glob("img_new_*.npy"))
            assert len(npy_files) == 4
    
    def test_run_skips_individual_tiles_when_disabled(self):
        """Test run() does not save individual files when save_individual=False."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
            )
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow.run()
            
            # Verify no .npy files were created
            npy_files = list(Path(tmpdir).glob("img_new_*.npy"))
            assert len(npy_files) == 0
    
    def test_run_cell_targeting_no_overview(self):
        """Test run_cell_targeting() handles missing overview gracefully."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        # Should not crash when no overview is available
        workflow.run_cell_targeting()
    
    def test_run_cell_targeting_with_overview(self):
        """Test run_cell_targeting() segments and targets cells."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        workflow._origin_stage_xy = (10000.0, 20000.0)
        
        # Create fake overview with some bright regions (cells)
        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:150, 100:150] = 0.8  # Bright region (cell 1)
        overview[300:350, 300:350] = 0.7  # Bright region (cell 2)
        
        found_calls = []
        started_calls = []
        done_calls = []
        
        def cells_found_cb(positions, n_cells):
            found_calls.append((positions, n_cells))
        
        def cell_started_cb(idx):
            started_calls.append(idx)
        
        def cell_done_cb(idx, success):
            done_calls.append((idx, success))
        
        workflow.run_cell_targeting(
            overview_image=overview,
            cells_found_cb=cells_found_cb,
            cell_started_cb=cell_started_cb,
            cell_done_cb=cell_done_cb,
        )
        
        # Verify cells were found
        assert len(found_calls) == 1
        assert found_calls[0][1] > 0  # At least one cell found
        
        # Verify recording was called for each cell (H + V)
        n_cells = found_calls[0][1]
        assert len(recording.run_calls) == n_cells * 2  # H and V for each cell
    
    def test_run_cell_targeting_applies_filter(self):
        """Test run_cell_targeting() applies segmentation filter."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        
        # Restrictive filter that should reject small regions
        seg_filter = {
            "area_um2_min": 50000.0,  # Very large minimum area (> 16,900 µm² for 20x20 @ 6.5µm)
            "area_enabled": True,
        }
        
        params = TilingParams(n_tiles=4)
        workflow = TilingWorkflow(facade, recording, params, seg_filter)
        workflow._origin_stage_xy = (10000.0, 20000.0)
        
        # Create fake overview with small bright regions
        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:120, 100:120] = 0.8  # Small region (~16,900 µm² at 6.5 µm/px)
        
        found_calls = []
        
        def cells_found_cb(positions, n_cells):
            found_calls.append((positions, n_cells))
        
        workflow.run_cell_targeting(
            overview_image=overview,
            pixel_size_um=6.5,
            cells_found_cb=cells_found_cb,
        )
        
        # Small region should be filtered out
        if len(found_calls) > 0:
            # Either no cells found, or cells found but filtered out
            assert found_calls[0][1] == 0
    
    def test_run_cell_targeting_moves_stage_to_cells(self):
        """Test run_cell_targeting() moves stage to each cell."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        workflow._origin_stage_xy = (10000.0, 20000.0)
        
        # Create fake overview with cells
        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:150, 100:150] = 0.8
        
        initial_move_count = facade.stage_con.move_to.call_count
        
        workflow.run_cell_targeting(overview_image=overview)
        
        # Verify stage moves occurred (at least once per cell found)
        assert facade.stage_con.move_to.call_count > initial_move_count
    
    def test_run_cell_targeting_calls_rotators(self):
        """Test run_cell_targeting() moves rotators for H and V."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        workflow._origin_stage_xy = (10000.0, 20000.0)
        
        # Create fake overview with one cell
        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:150, 100:150] = 0.8
        
        workflow.run_cell_targeting(overview_image=overview)
        
        # Verify rotator moves for H and V
        assert facade.rotator_qwp.chained_move_to_h.call_count > 0
        assert facade.rotator_qwp.chained_move_to_v.call_count > 0
    
    def test_grab_image_hw_fallback_on_timeout(self):
        """Test _grab_image_hw falls back to software on timeout."""
        facade = build_mock_facade()
        facade.cam.wait_for_frame = MagicMock(return_value=False)  # Timeout
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        # Should fall back to software acquisition
        img = workflow._grab_image_hw(laser_pin=0, camera_pin=1)
        assert img is not None
        assert img.shape == (512, 512)
    
    def test_grab_image_retry_logic(self):
        """Test _grab_image retries on initial failure."""
        facade = build_mock_facade()
        
        # First call returns None, second call returns data
        call_count = [0]
        
        def get_data_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # First call fails
            fake_frame = np.random.randint(0, 65535, (512, 512), dtype=np.uint16)
            return np.array([fake_frame])
        
        facade.cam.get_data = MagicMock(side_effect=get_data_side_effect)
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        # Should retry and succeed on second attempt
        img = workflow._grab_image()
        assert img is not None
        assert img.shape == (512, 512)
    
    def test_grab_image_max_retries(self):
        """Test _grab_image raises after max retries."""
        facade = build_mock_facade()
        facade.cam.get_data = MagicMock(return_value=None)  # Always fail
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        with pytest.raises(RuntimeError, match="failed to acquire image"):
            workflow._grab_image()
    
    def test_prepare_h5_removes_old_files(self):
        """Test _prepare_h5 removes existing files."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # Create old files
            (tmpdir / "Tiling_measurement.h5").touch()
            (tmpdir / "img_new_0_0.npy").touch()
            (tmpdir / "img_new_1_0.npy").touch()
            
            workflow = TilingWorkflow(facade, recording, params)
            workflow._prepare_h5(tmpdir)
            
            # Verify old files were removed
            assert not (tmpdir / "Tiling_measurement.h5").exists()
            assert not (tmpdir / "img_new_0_0.npy").exists()
            assert not (tmpdir / "img_new_1_0.npy").exists()
    
    def test_get_stage_position(self):
        """Test _get_stage_position returns facade position."""
        facade = build_mock_facade()
        facade.stage_con.get_position = MagicMock(return_value=(12345.0, 67890.0))
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        pos = workflow._get_stage_position()
        
        assert pos == (12345.0, 67890.0)
    
    def test_segment_cells_no_future_warning(self):
        """_segment_cells must not raise FutureWarning from skimage deprecations."""
        import warnings

        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)

        workflow = TilingWorkflow(facade, recording, params)

        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:200, 100:200] = 0.8

        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            workflow._segment_cells(overview, pixel_size_um=6.5)

    def test_segment_cells_returns_dict(self):
        """Test _segment_cells returns proper dict structure."""
        facade = build_mock_facade()
        recording = MockRecordingWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, recording, params)
        
        # Create simple test image with bright region
        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:200, 100:200] = 0.8
        
        result = workflow._segment_cells(overview, pixel_size_um=6.5)
        
        # Should return dict with expected keys
        assert isinstance(result, dict)
        if len(result) > 0:  # If cells were found
            assert "centroid_x_um" in result
            assert "centroid_y_um" in result
            assert "area_um2" in result
            assert "mean_intensity" in result
            assert "max_intensity" in result
            assert "eccentricity" in result

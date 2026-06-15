"""Unit tests for TilingWorkflow."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import TilingParams, TilingWorkflow


class MockWidefieldStarssWorkflow:
    """Mock WidefieldStarss workflow for testing."""
    
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
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, widefield_starss, params)
        assert workflow.facade is facade
        assert workflow._recording is widefield_starss
        assert workflow.params is params
        assert workflow.seg_filter == {}
    
    def test_workflow_with_seg_filter(self):
        """Test TilingWorkflow with segmentation filter."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        seg_filter = {
            "area_um2_min": 100.0,
            "area_um2_max": 1000.0,
            "area_enabled": True,
        }
        
        workflow = TilingWorkflow(facade, widefield_starss, params, seg_filter)
        assert workflow.seg_filter == seg_filter
    
    def test_workflow_initialization_without_widefield_starss_workflow(self):
        """Test backward-compatible TilingWorkflow(facade, params) initialization."""
        facade = build_mock_facade()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, params)

        assert workflow.facade is facade
        assert workflow._recording is None
        assert workflow.params is params

    def test_run_creates_default_save_folder(self, monkeypatch, tmp_path):
        """Test run() creates a timestamped folder from measurements_root."""
        facade = build_mock_facade()
        params = TilingParams(
            n_tiles=1,
            measurements_root=tmp_path,
            save_individual=False,
            pulsed=False,
        )
        workflow = TilingWorkflow(facade, params)

        monkeypatch.setattr("imswitch.imcontrol.model.workflows.tiling.time.sleep", lambda _seconds: None)
        workflow.run()

        assert workflow.params.save_folder is not None
        assert workflow.params.save_folder.is_relative_to(tmp_path)
        assert workflow.params.save_folder.exists()
    
    def test_run_creates_tiles(self):
        """Test run() executes tiling scan."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=True,
                pulsed=False,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
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
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # 5 tiles should round to 9 (3x3)
            params = TilingParams(
                n_tiles=5,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=False,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run()
            
            # Verify 9 tiles were acquired (3x3 spiral) + 1 move to origin
            assert facade.stage_con.move_to.call_count == 10
    
    def test_run_hardware_triggered_mode(self):
        """Test run() uses hardware triggering when configured."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=True,
                laser_pin=2,
                camera_pin=3,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run()
            
            # Verify triggered mode was set
            facade.laser_con.set_triggered_mode.assert_called_once()
            
            # Verify snap_trigger was called for each tile
            assert facade.trig.snap_trigger.call_count == 4
    
    def test_run_software_pulsed_mode(self):
        """Test run() uses software pulsing when no trigger available."""
        facade = build_mock_facade()
        facade.trig.connected = False
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
                pulsed=True,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run()
            
            # Verify constant power mode was used
            facade.laser_con.set_constant_power.assert_called_once()
            
            # Verify laser on/off calls (4 tiles + 1 initial off after set_constant_power)
            assert facade.laser_con.laser_on.call_count == 4
            assert facade.laser_con.laser_off.call_count == 5
    
    def test_run_tile_callback(self):
        """Test run() calls tile_callback for each tile."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        
        callback_calls = []
        
        def tile_callback(image, gx, gy, idx, n_total):
            callback_calls.append((gx, gy, idx, n_total))
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run(tile_callback=tile_callback)
            
            # Verify callback was called for each tile
            assert len(callback_calls) == 4
            
            # Check that n_total is correct in all calls
            assert all(call[3] == 4 for call in callback_calls)
    
    def test_run_saves_individual_tiles(self):
        """Test run() saves individual .npy files when configured."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=True,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run()
            
            # Verify .npy files were created
            npy_files = list(Path(tmpdir).glob("img_new_*.npy"))
            assert len(npy_files) == 4
    
    def test_run_skips_individual_tiles_when_disabled(self):
        """Test run() does not save individual files when save_individual=False."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            params = TilingParams(
                n_tiles=4,
                save_folder=Path(tmpdir),
                save_individual=False,
            )
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow.run()
            
            # Verify no .npy files were created
            npy_files = list(Path(tmpdir).glob("img_new_*.npy"))
            assert len(npy_files) == 0
    
    def _stitched_with_blob(self) -> object:
        """Helper: build a real StitchedImage containing a single bright tile."""
        from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage
        tile = np.zeros((128, 128), dtype=np.float32)
        tile[40:90, 40:90] = 0.8
        s = StitchedImage(
            tile_size_px=128,
            tile_step_um=64.0,
            tile_shape_px=(128, 128),
            pixel_size_um=0.5,
        )
        s.add_tile(tile, 0, 0)
        return s

    def _controller_with_blob_stitcher(self) -> tuple[object, MagicMock]:
        """Build a TilingController shell around a stitched overview."""
        from qtpy import QtCore

        from imswitch.imcontrol.controller.controllers.TilingController import (
            TilingController,
        )

        controller = TilingController.__new__(TilingController)
        QtCore.QObject.__init__(controller)
        controller._stitcher = self._stitched_with_blob()
        controller._originXY = (100.0, 200.0)
        controller._gridPositions = [(0, 0)]
        controller._lastStepUm = 64.0
        controller._segParams = {}
        controller._cellPositionsRC = None
        controller._cellProps = None
        controller._cellTargetingRunning = False
        controller._logger = MagicMock()

        positioner = MagicMock()
        controller._master = SimpleNamespace(positionersManager={"xy": positioner})
        controller._setupInfo = SimpleNamespace(
            tiling=SimpleNamespace(xyPositioner="xy"),
            positioners={"xy": SimpleNamespace(axes=("x", "y"))},
        )
        return controller, positioner

    def test_run_cell_targeting_invokes_for_each_feature(self):
        """run_cell_targeting iterates accepted cells through for_each_feature."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        workflow = TilingWorkflow(facade, widefield_starss, params)

        stitched = self._stitched_with_blob()
        seen = []

        workflow.run_cell_targeting(
            stitched=stitched,
            pixel_size_um=0.5,
            canvas_origin_stage=(0.0, 0.0),
            for_each_feature=lambda i, props, xy: seen.append((i, xy)),
        )

        assert len(seen) >= 1
        assert facade.stage_con.move_to.call_count >= len(seen)

    def test_run_cell_targeting_filter_rejects_small(self):
        """Restrictive area filter yields zero accepted cells."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        seg_filter = {"area_enabled": True, "area_um2_min": 1e9, "area_um2_max": 1e10}
        workflow = TilingWorkflow(facade, widefield_starss, params, seg_filter)

        stitched = self._stitched_with_blob()
        found = []

        workflow.run_cell_targeting(
            stitched=stitched,
            pixel_size_um=0.5,
            canvas_origin_stage=(0.0, 0.0),
            cells_found_cb=lambda positions, n: found.append(n),
        )

        assert found == [0]

    def test_controller_detect_cell_targets_does_not_move_stage(self) -> None:
        """GUI-safe cell detection should only show/cache markers."""
        controller, positioner = self._controller_with_blob_stitcher()

        positions = controller.detectCellTargets()

        assert len(positions) >= 1
        positioner.setPosition.assert_not_called()
        assert controller._cellPositionsRC is not None

    def test_controller_run_cell_targeting_without_callback_does_not_move(
        self,
    ) -> None:
        """runCellTargeting without an explicit workflow is detection-only."""
        controller, positioner = self._controller_with_blob_stitcher()

        controller.runCellTargeting()

        positioner.setPosition.assert_not_called()
        assert controller._cellPositionsRC is not None

    def test_controller_run_cell_targeting_moves_only_when_explicit(
        self, monkeypatch
    ) -> None:
        """Automated controller path moves and invokes the per-cell callback."""
        import importlib

        module = importlib.import_module(
            "imswitch.imcontrol.controller.controllers.TilingController"
        )

        class ImmediateThread:
            def __init__(
                self, target: Any, args: tuple = (), daemon: bool | None = None
            ) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

        monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
        monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
        controller, positioner = self._controller_with_blob_stitcher()
        seen = []

        controller.runCellTargeting(
            feature_callback=lambda i, props, xy: seen.append((i, props, xy))
        )

        assert len(seen) >= 1
        assert positioner.setPosition.call_count >= 2 * len(seen)
        assert isinstance(seen[0][1], dict)
        assert len(seen[0][2]) == 2


    def test_grab_image_hw_fallback_on_timeout(self):
        """Test _grab_image_hw falls back to software on timeout."""
        facade = build_mock_facade()
        facade.cam.wait_for_frame = MagicMock(return_value=False)  # Timeout
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, widefield_starss, params)
        
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
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, widefield_starss, params)
        
        # Should retry and succeed on second attempt
        img = workflow._grab_image()
        assert img is not None
        assert img.shape == (512, 512)
    
    def test_grab_image_max_retries(self):
        """Test _grab_image raises after max retries."""
        facade = build_mock_facade()
        facade.cam.get_data = MagicMock(return_value=None)  # Always fail
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=1)
        
        workflow = TilingWorkflow(facade, widefield_starss, params)
        
        with pytest.raises(RuntimeError, match="failed to acquire image"):
            workflow._grab_image()
    
    def test_prepare_h5_removes_old_files(self):
        """Test _prepare_h5 removes existing files."""
        facade = build_mock_facade()
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # Create old files
            (tmpdir / "Tiling_measurement.h5").touch()
            (tmpdir / "img_new_0_0.npy").touch()
            (tmpdir / "img_new_1_0.npy").touch()
            
            workflow = TilingWorkflow(facade, widefield_starss, params)
            workflow._prepare_h5(tmpdir)
            
            # Verify old files were removed
            assert not (tmpdir / "Tiling_measurement.h5").exists()
            assert not (tmpdir / "img_new_0_0.npy").exists()
            assert not (tmpdir / "img_new_1_0.npy").exists()
    
    def test_get_stage_position(self):
        """Test _get_stage_position returns facade position."""
        facade = build_mock_facade()
        facade.stage_con.get_position = MagicMock(return_value=(12345.0, 67890.0))
        widefield_starss = MockWidefieldStarssWorkflow()
        params = TilingParams(n_tiles=4)
        
        workflow = TilingWorkflow(facade, widefield_starss, params)
        pos = workflow._get_stage_position()
        
        assert pos == (12345.0, 67890.0)
    
    def test_segmenter_no_future_warning(self):
        """Segmenter must not raise FutureWarning from skimage deprecations."""
        import warnings
        from imswitch.imcontrol.model.workflows.segmentation import Segmenter

        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:200, 100:200] = 0.8

        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            Segmenter(threshold=0.3).segment(overview, pixel_size_um=6.5)

    def test_segmenter_returns_dict(self):
        """Segmenter.segment returns the documented dict schema."""
        from imswitch.imcontrol.model.workflows.segmentation import Segmenter

        overview = np.zeros((512, 512), dtype=np.float32)
        overview[100:200, 100:200] = 0.8

        result = Segmenter(threshold=0.3).segment(overview, pixel_size_um=6.5)

        assert isinstance(result, dict)
        if result:
            for key in (
                "centroid_row", "centroid_col",
                "centroid_x_um", "centroid_y_um",
                "area_um2", "mean_intensity", "max_intensity", "eccentricity",
            ):
                assert key in result

    def test_segmenter_reuses_shared_improcess_kernel(self):
        """Tiling Segmenter remains a wrapper around the shared kernel."""
        from imswitch.imcontrol.model.workflows.segmentation import Segmenter
        from imswitch.improcess.analysis.segmentation import segment_image

        overview = np.zeros((64, 64), dtype=np.float32)
        overview[20:44, 20:44] = 0.8
        pixel_size_um = 0.5

        segmenter = Segmenter(
            blur_sigma_px=0.0,
            threshold=0.3,
            min_area_px=10,
            peak_min_dist_px=8,
        )
        props = segmenter.segment(overview, pixel_size_um=pixel_size_um)
        expected = segment_image(
            overview,
            threshold_method="manual",
            threshold_value=0.3,
            min_area=10,
            smooth_sigma=0.0,
            normalize=True,
            label_method="watershed",
            watershed_min_distance=8,
            pixel_size_um=pixel_size_um,
        )

        np.testing.assert_array_equal(segmenter.labels, expected.labels)
        np.testing.assert_array_equal(props["label"], [region.label for region in expected.regions])
        np.testing.assert_allclose(
            props["area_um2"],
            [region.area_um2 for region in expected.regions],
        )

    def test_detect_cell_targets_helper_centralizes_filtering(self):
        """Helper returns full props, valid indices and filtered positions."""
        from imswitch.imcontrol.model.workflows.segmentation import detect_cell_targets

        overview = np.zeros((64, 64), dtype=np.float32)
        overview[20:44, 20:44] = 0.8

        accepted = detect_cell_targets(
            overview,
            pixel_size_um=0.5,
            params={
                "blur_sigma_px": 0.0,
                "threshold": 0.3,
                "min_area_px": 10,
            },
        )
        assert accepted.n_total >= 1
        assert accepted.n_valid == accepted.n_total
        assert accepted.positions.shape == (accepted.n_valid, 2)
        assert len(accepted.filtered_props["label"]) == accepted.n_valid

        rejected = detect_cell_targets(
            overview,
            pixel_size_um=0.5,
            params={
                "blur_sigma_px": 0.0,
                "threshold": 0.3,
                "min_area_px": 10,
                "area_enabled": True,
                "area_um2_min": 1e9,
                "area_um2_max": 1e10,
            },
        )
        assert rejected.n_total >= 1
        assert rejected.n_valid == 0
        assert rejected.positions.shape == (0, 2)
        assert len(rejected.filtered_props["label"]) == 0

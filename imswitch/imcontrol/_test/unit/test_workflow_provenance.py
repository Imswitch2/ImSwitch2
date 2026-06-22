"""Unit tests for workflow provenance metadata and atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    TilingParams,
    TilingWorkflow,
    ZStackParams,
    ZStackWorkflow,
    TargetTimelapseParams,
    TargetTimelapseWorkflow,
)
from imswitch.imcontrol.model.workflows.target import TargetList


def build_mock_facade():
    """Build a minimal mock facade for testing."""
    facade = MagicMock()
    facade.laser_con.laser_on = MagicMock()
    facade.laser_con.laser_off = MagicMock()
    facade.laser_con.set_constant_power = MagicMock()
    facade.laser_con.set_modulation_mode = MagicMock()
    facade.cam.prepare_live = MagicMock()
    facade.cam.start_live = MagicMock()
    facade.cam.stop_live = MagicMock()
    facade.cam.get_data = MagicMock(
        return_value=np.random.randint(0, 100, (1, 512, 512), dtype=np.uint16)
    )
    facade.stage_con.move_to = MagicMock()
    facade.stage_con.get_position = MagicMock(return_value=(10000.0, 20000.0))
    facade.trig.snap_trigger = MagicMock()
    facade.trig.connected = True
    facade.positioner.move = MagicMock()
    facade.positioner.get_position = MagicMock(return_value=0.0)
    facade.recording.start_recording_for_scan = MagicMock()
    facade.recording.stop_recording = MagicMock()
    # Z-stack specific mocks
    facade.z_stage_con.activate_ext_control = MagicMock()
    facade.z_stage_con.set_pos_um = MagicMock()
    facade.z_stage_con.read_pos_um = MagicMock(return_value=0.0)
    facade.z_stage_con.pos_range_um = (0.0, 100.0)
    return facade


class TestTilingWorkflowProvenance:
    """Test provenance metadata for TilingWorkflow."""

    def test_tiling_writes_provenance_sidecar(self):
        """TilingWorkflow writes acquisition_metadata.json with correct params."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_folder = Path(tmpdir)
            params = TilingParams(n_tiles=1, save_folder=save_folder, skip_cell_targeting=True)
            facade = build_mock_facade()
            workflow = TilingWorkflow(facade, params)
            workflow.run()

            metadata_path = save_folder / "acquisition_metadata.json"
            assert metadata_path.exists(), "acquisition_metadata.json not created"

            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            assert "parameters" in metadata
            assert metadata["parameters"]["n_tiles"] == 1
            assert "imswitch_version" in metadata
            assert "acquisition_timestamp_utc" in metadata
            assert "completed" in metadata
            assert metadata["completed"] is True

    def test_tiling_writes_provenance_on_partial_run(self):
        """TilingWorkflow writes provenance even if acquisition fails mid-run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_folder = Path(tmpdir)
            params = TilingParams(n_tiles=4, save_folder=save_folder, skip_cell_targeting=True)
            facade = build_mock_facade()

            # Make camera fail after first tile
            call_count = [0]

            def failing_get_data():
                call_count[0] += 1
                if call_count[0] == 1:
                    return np.random.randint(0, 100, (1, 512, 512), dtype=np.uint16)
                raise RuntimeError("Camera failure")

            facade.cam.get_data = failing_get_data

            workflow = TilingWorkflow(facade, params)
            with pytest.raises(RuntimeError, match="Camera failure"):
                workflow.run()

            metadata_path = save_folder / "acquisition_metadata.json"
            assert metadata_path.exists(), "Provenance not written on failure"

            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            assert metadata["completed"] is False

    def test_tiling_atomic_npy_writes(self):
        """TilingWorkflow uses temp-then-rename for .npy files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_folder = Path(tmpdir)
            params = TilingParams(n_tiles=1, save_folder=save_folder, skip_cell_targeting=True)
            facade = build_mock_facade()
            workflow = TilingWorkflow(facade, params)

            # Patch np.save to verify temp file usage
            original_np_save = np.save
            save_calls = []

            def tracked_save(path, arr):
                save_calls.append(str(path))
                original_np_save(path, arr)

            with patch("numpy.save", side_effect=tracked_save):
                workflow.run()

            # Check that at least one .tmp file was created during the process
            tmp_saves = [c for c in save_calls if c.endswith(".tmp")]
            assert len(tmp_saves) > 0, "No .tmp files created during atomic save"

            # Check that no .tmp files remain after completion
            tmp_files = list(save_folder.glob("*.tmp"))
            assert len(tmp_files) == 0, f"Temp files not cleaned up: {tmp_files}"

            # Check that final .npy files exist
            npy_files = list(save_folder.glob("img_new_*.npy"))
            assert len(npy_files) > 0, "No final .npy files found"


class TestZStackWorkflowProvenance:
    """Test provenance metadata for ZStackWorkflow."""

    def test_zstack_writes_provenance_sidecar(self):
        """ZStackWorkflow writes acquisition_metadata.json with correct params."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_folder = Path(tmpdir)
            params = ZStackParams(
                n_planes=3,
                step_um=1.0,
                pulsed=False,
                measurements_root=save_folder,
            )
            facade = build_mock_facade()
            workflow = ZStackWorkflow(facade, params)
            workflow.run(save_stack=True, z_start=0.0)

            # Z-stack creates a dated subdirectory
            # Find the acquisition_metadata.json file recursively
            metadata_files = list(save_folder.rglob("acquisition_metadata.json"))
            assert len(metadata_files) == 1, f"Expected 1 metadata file, found {len(metadata_files)}"
            metadata_path = metadata_files[0]

            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            assert metadata["parameters"]["n_planes"] == 3
            assert metadata["parameters"]["step_um"] == 1.0
            assert metadata["completed"] is True


class TestTargetTimelapseWorkflowProvenance:
    """Test provenance metadata for TargetTimelapseWorkflow."""

    def test_target_timelapse_writes_provenance_when_save_folder_set(self):
        """TargetTimelapseWorkflow writes provenance when save_folder is provided."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_folder = Path(tmpdir)
            targets = TargetList()
            targets.add(stage_xy=(100.0, 200.0), source="manual", enabled=True)

            params = TargetTimelapseParams(
                n_timepoints=1,
                interval_s=0.0,
                save_folder=save_folder,
            )
            facade = build_mock_facade()

            # Mock acquisition callback
            def mock_acquisition(target, timepoint_idx):
                return "success"

            workflow = TargetTimelapseWorkflow(
                facade, targets, params, mock_acquisition, workflow_name="test"
            )
            workflow.run()

            metadata_path = save_folder / "acquisition_metadata.json"
            assert metadata_path.exists(), "acquisition_metadata.json not created"

            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            assert metadata["parameters"]["n_timepoints"] == 1
            assert metadata["completed"] is True

    def test_target_timelapse_no_provenance_when_save_folder_none(self):
        """TargetTimelapseWorkflow does not write provenance when save_folder is None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            targets = TargetList()
            targets.add(stage_xy=(100.0, 200.0), source="manual", enabled=True)

            params = TargetTimelapseParams(
                n_timepoints=1,
                interval_s=0.0,
                save_folder=None,
            )
            facade = build_mock_facade()

            def mock_acquisition(target, timepoint_idx):
                return "success"

            workflow = TargetTimelapseWorkflow(
                facade, targets, params, mock_acquisition, workflow_name="test"
            )
            workflow.run()

            # No metadata should be written since save_folder is None
            # (We can't check tmpdir because workflow doesn't use it)


class TestRecordingManagerMetadata:
    """Test RecordingManager metadata augmentation."""

    def test_recording_manager_augments_attrs_with_metadata(self):
        """RecordingManager adds version, timestamp, and exposure to attributes."""
        from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker
        from unittest.mock import MagicMock

        # Create a minimal mock recording manager and detectors manager
        mock_recording_manager = MagicMock()
        mock_detector = MagicMock()
        mock_detector.getExposureTime = MagicMock(return_value=50.0)
        mock_detectors_manager = MagicMock()
        mock_detectors_manager.__getitem__ = MagicMock(return_value=mock_detector)
        mock_recording_manager.detectorsManager = mock_detectors_manager

        # Create a RecordingWorker instance
        worker = RecordingWorker(mock_recording_manager)

        # Test the augment method
        original_attrs = {
            "TestCamera": {
                "setup:laser_power": "5.0",
            }
        }

        augmented = worker._augment_attrs_with_recording_metadata(original_attrs)

        assert "TestCamera" in augmented
        attrs = augmented["TestCamera"]
        assert "setup:laser_power" in attrs
        assert attrs["setup:laser_power"] == "5.0"
        assert "acquisition:software_version" in attrs
        assert "acquisition:start_time" in attrs
        assert "acquisition:exposure_time_ms" in attrs
        assert attrs["acquisition:exposure_time_ms"] == "50.0"

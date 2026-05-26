"""Unit tests for RecordingWorkflow — polarisation-resolved acquisition."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile as tf

from imswitch.imcontrol.model.workflows import (
    RecordingParams,
    RecordingWorkflow,
    build_mock_facade,
)


@pytest.fixture
def mock_facade():
    """Provide a MockMicroscopeFacade with canned camera data."""
    facade = build_mock_facade()
    # Create a synthetic polarisation stack: 10 frames (5 signal + 5 background pairs)
    # Shape: (10, 64, 64) — matches typical 2×frame_number structure
    stack = np.random.randint(100, 5000, size=(10, 64, 64), dtype=np.uint16)
    facade.cam.set_canned_data(stack)
    return facade


@pytest.fixture
def default_params():
    """Provide default RecordingParams for a typical run."""
    return RecordingParams(
        pin488=1,
        pin405=2,
        camerapin=3,
        start488=100,
        start405=200,
        start_camera=50,
        width488=1000,
        width405=1000,
        width_camera=10000,
        dwelltime=20000,
        delay_time=100,
        frame_number=5,  # Will result in 10 total frames (2×5)
        move_waveplate=True,
        record_h=True,
        record_v=True,
    )


class TestRecordingWorkflow:
    """Test suite for RecordingWorkflow."""

    def test_import(self):
        """Verify RecordingWorkflow and RecordingParams are importable."""
        from imswitch.imcontrol.model.workflows import (
            RecordingParams,
            RecordingWorkflow,
        )
        assert RecordingWorkflow is not None
        assert RecordingParams is not None

    def test_workflow_initialization(self, mock_facade, default_params):
        """Verify workflow can be instantiated with facade and params."""
        workflow = RecordingWorkflow(mock_facade, default_params)
        assert workflow.facade is mock_facade
        assert workflow.params is default_params
        assert isinstance(workflow.measurements_root, Path)
        assert workflow.datastack is None
        assert workflow.datastack_h is None
        assert workflow.datastack_v is None

    def test_none_measurements_root_uses_environment_default(
        self, mock_facade, default_params, monkeypatch, tmp_path
    ):
        """measurements_root=None should resolve before any Path arithmetic."""
        monkeypatch.setenv("IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT", str(tmp_path))
        default_params.measurements_root = None

        workflow = RecordingWorkflow(mock_facade, default_params)

        assert workflow.measurements_root == tmp_path

    def test_string_measurements_root_is_converted_to_path(
        self, mock_facade, default_params, tmp_path
    ):
        """String roots should be accepted and normalized to Path."""
        default_params.measurements_root = str(tmp_path)

        workflow = RecordingWorkflow(mock_facade, default_params)

        assert workflow.measurements_root == tmp_path

    def test_both_polarisations_recorded(self, mock_facade, default_params):
        """Verify H and V stacks are both acquired when both flags are True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # Check that both H and V stacks were saved
            assert workflow.datastack_h is not None
            assert workflow.datastack_v is not None
            assert workflow.datastack_h.shape == (10, 64, 64)
            assert workflow.datastack_v.shape == (10, 64, 64)

            # Verify facade calls include both polarisations
            call_names = mock_facade.call_names()
            assert "rotator_qwp.move_to_h" in call_names
            assert "rotator_qwp.move_to_v" in call_names
            assert call_names.count("cam.prepare_acquisition") == 2
            assert call_names.count("cam.start_acquisition") == 2
            assert call_names.count("trig.Sendsignal") == 2
            assert call_names.count("cam.stop_acquisition") == 2

    def test_only_h_polarisation(self, mock_facade, default_params):
        """Verify only H stack is acquired when record_v=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.record_v = False
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            assert workflow.datastack_h is not None
            assert workflow.datastack_v is None

            call_names = mock_facade.call_names()
            assert "rotator_qwp.move_to_h" in call_names
            assert "rotator_qwp.move_to_v" not in call_names
            assert call_names.count("cam.prepare_acquisition") == 1

    def test_only_v_polarisation(self, mock_facade, default_params):
        """Verify only V stack is acquired when record_h=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.record_h = False
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            assert workflow.datastack_h is None
            assert workflow.datastack_v is not None

            call_names = mock_facade.call_names()
            assert "rotator_qwp.move_to_h" not in call_names
            assert "rotator_qwp.move_to_v" in call_names
            assert call_names.count("cam.prepare_acquisition") == 1

    def test_no_op_when_both_false(self, mock_facade, default_params):
        """Verify workflow is a no-op when both record_h and record_v are False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.record_h = False
            default_params.record_v = False
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # No acquisitions should have been triggered
            assert workflow.datastack_h is None
            assert workflow.datastack_v is None
            call_names = mock_facade.call_names()
            assert "cam.prepare_acquisition" not in call_names
            assert "trig.Sendsignal" not in call_names

    def test_tiff_files_saved(self, mock_facade, default_params):
        """Verify TIFF files are saved with correct names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.measurement_name_addition = "_test"
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # Check that TIFF files were created
            saved_files = list(Path(tmpdir).rglob("*.tif"))
            assert len(saved_files) == 2  # One for H, one for V

            # Verify filenames contain polarisation labels
            filenames = [f.name for f in saved_files]
            assert any("_h.tif" in name for name in filenames)
            assert any("_v.tif" in name for name in filenames)

    def test_tiff_content_valid(self, mock_facade, default_params):
        """Verify saved TIFF files can be read and have correct shape."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            saved_files = list(Path(tmpdir).rglob("*.tif"))
            for tiff_path in saved_files:
                loaded = tf.imread(str(tiff_path))
                assert loaded.shape == (10, 64, 64)
                assert loaded.dtype == np.uint16

    def test_waveplate_movement_disabled(self, mock_facade, default_params):
        """Verify waveplates are not moved when move_waveplate=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.move_waveplate = False
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            call_names = mock_facade.call_names()
            assert "rotator_qwp.move_to_h" not in call_names
            assert "rotator_qwp.move_to_v" not in call_names
            # But acquisitions should still happen
            assert "cam.prepare_acquisition" in call_names

    def test_laser_modulation_disabled(self, mock_facade, default_params):
        """Verify laser modulation is disabled at workflow start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # First call should be set_modulation_mode(None)
            first_call = mock_facade.calls[0]
            assert first_call[0] == "laser_con.set_modulation_mode"
            assert first_call[1] == (None,)

    def test_trigger_command_called_once(self, mock_facade, default_params):
        """Verify trig.command is called once and reused for both polarisations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # trig.command should be called exactly once (pulse scheme computed once)
            command_calls = [c for c in mock_facade.calls if c[0] == "trig.command"]
            assert len(command_calls) == 1

            # But Sendsignal should be called twice (once per polarisation)
            sendsignal_calls = [c for c in mock_facade.calls if c[0] == "trig.Sendsignal"]
            assert len(sendsignal_calls) == 2

    def test_calculate_r_produces_numeric_result(self):
        """Verify _calculate_r computes anisotropy without crashing."""
        # Create a synthetic stack with known structure
        stack = np.random.randint(100, 5000, size=(10, 64, 64), dtype=np.uint16)
        
        # Should not raise an exception
        RecordingWorkflow._calculate_r(stack)
        # Note: _calculate_r returns None (just logs), so we only verify no crash

    def test_compute_anisotropy_map_returns_valid_shape(self):
        """Verify compute_anisotropy_map produces correct output shape."""
        stack = np.random.randint(100, 5000, size=(10, 64, 64), dtype=np.uint16)
        r_map = RecordingWorkflow.compute_anisotropy_map(stack)
        
        # Output should be downsampled by factor of 2 in each spatial dimension
        assert r_map.shape == (32, 32)
        assert r_map.dtype == np.float32

    def test_compute_full_anisotropy_map_h_v(self):
        """Verify compute_full_anisotropy_map processes H and V stacks."""
        stack_h = np.random.randint(100, 5000, size=(10, 64, 64), dtype=np.uint16)
        stack_v = np.random.randint(100, 5000, size=(10, 64, 64), dtype=np.uint16)
        
        r_map = RecordingWorkflow.compute_full_anisotropy_map(stack_h, stack_v)
        
        assert r_map.shape == (32, 32)
        assert r_map.dtype == np.float32
        # Some pixels may be NaN due to weak signal, but not all
        assert not np.all(np.isnan(r_map))

    def test_custom_measurements_root(self, mock_facade, default_params):
        """Verify measurements_root can be overridden in constructor."""
        with tempfile.TemporaryDirectory() as tmpdir1, \
             tempfile.TemporaryDirectory() as tmpdir2:
            default_params.measurements_root = Path(tmpdir1)
            # Constructor override should take precedence
            workflow = RecordingWorkflow(
                mock_facade, default_params, measurements_root=Path(tmpdir2)
            )
            workflow.run()

            # Files should be in tmpdir2, not tmpdir1
            assert len(list(Path(tmpdir2).rglob("*.tif"))) == 2
            assert len(list(Path(tmpdir1).rglob("*.tif"))) == 0

    def test_measurement_name_addition_in_filename(self, mock_facade, default_params):
        """Verify measurement_name_addition appears in saved filenames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.measurement_name_addition = "_myexperiment"
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            saved_files = list(Path(tmpdir).rglob("*.tif"))
            filenames = [f.name for f in saved_files]
            assert any("_myexperiment_h.tif" in name for name in filenames)
            assert any("_myexperiment_v.tif" in name for name in filenames)

    def test_camera_frame_count_correct(self, mock_facade, default_params):
        """Verify camera is prepared for 2×frame_number frames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            default_params.measurements_root = Path(tmpdir)
            default_params.frame_number = 7
            # Update mock data to match
            mock_facade.cam.set_canned_data(
                np.random.randint(100, 5000, size=(14, 64, 64), dtype=np.uint16)
            )
            workflow = RecordingWorkflow(mock_facade, default_params)
            workflow.run()

            # Check prepare_acquisition calls
            prep_calls = [c for c in mock_facade.calls if c[0] == "cam.prepare_acquisition"]
            # Each polarisation should request 2 × frame_number = 14 frames
            for call in prep_calls:
                assert call[1] == (14,)


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

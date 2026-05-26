"""Tests for ZStackWorkflow — headless Z-stack acquisition."""

import numpy as np
import pytest
import tifffile as tf
from pathlib import Path

from imswitch.imcontrol.model.workflows import (
    ZStackParams,
    ZStackWorkflow,
    build_mock_facade,
)


@pytest.mark.nohardware
def test_zstack_software_mode():
    """Z-stack in software (live) mode: correct number of frames, return to start."""
    facade = build_mock_facade()
    
    # Mock camera returns 10 planes of 64×64 frames
    n_planes = 10
    mock_frames = np.random.randint(0, 1000, (n_planes, 64, 64), dtype=np.uint16)
    facade.cam.set_canned_data(mock_frames)
    
    params = ZStackParams(
        n_planes=n_planes,
        step_um=1.0,
        pulsed=False,
        laser_power_488_mw=50.0,
        exposure_us=50000.0,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=False, z_start=50.0)
    
    # Verify stack shape
    assert stack.shape == (n_planes, 64, 64)
    assert len(z_positions) == n_planes
    
    # Verify z positions are centered around z_start
    assert z_positions[0] == pytest.approx(50.0 - (n_planes - 1) * 0.5, abs=0.01)
    assert z_positions[-1] == pytest.approx(50.0 + (n_planes - 1) * 0.5, abs=0.01)
    
    # Verify method call sequence
    call_names = facade.call_names()
    assert "z_stage_con.activate_ext_control" in call_names
    assert "laser_con.set_constant_power" in call_names
    assert call_names.count("z_stage_con.set_pos_um") == n_planes + 2  # n_planes + start + return
    assert call_names.count("cam.prepare_live") == n_planes
    assert call_names.count("cam.start_live") == n_planes
    assert call_names.count("cam.stop_live") == n_planes
    assert "laser_con.set_modulation_mode" in call_names
    
    # Verify return to start position
    set_pos_calls = [c for c in facade.calls if c[0] == "z_stage_con.set_pos_um"]
    assert set_pos_calls[-1][1][0] == pytest.approx(50.0, abs=0.01)


@pytest.mark.nohardware
def test_zstack_save_uses_default_root_when_params_root_is_none(monkeypatch, tmp_path):
    """Z-stack saving should accept measurements_root=None."""
    facade = build_mock_facade()
    params = ZStackParams(n_planes=1, step_um=1.0, measurements_root=None)
    workflow = ZStackWorkflow(facade, params)
    saved_paths = []

    monkeypatch.setenv("IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT", str(tmp_path))
    monkeypatch.setattr(tf, "imwrite", lambda path, stack: saved_paths.append(path))

    workflow._save(np.zeros((1, 4, 4), dtype=np.uint16))

    assert len(saved_paths) == 1
    assert Path(saved_paths[0]).is_relative_to(tmp_path)


@pytest.mark.nohardware
def test_zstack_hardware_triggered_mode():
    """Z-stack in hardware-triggered mode: uses trig.snap_trigger."""
    facade = build_mock_facade()
    
    n_planes = 5
    mock_frames = np.random.randint(0, 1000, (n_planes, 128, 128), dtype=np.uint16)
    facade.cam.set_canned_data(mock_frames)
    
    params = ZStackParams(
        n_planes=n_planes,
        step_um=2.0,
        pulsed=True,
        laser_pin=3,
        camera_pin=4,
        laser_power_488_mw=100.0,
        exposure_us=100000.0,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=False, z_start=60.0)
    
    # Verify triggered mode was set
    call_names = facade.call_names()
    assert "laser_con.set_triggered_mode" in call_names
    assert call_names.count("trig.snap_trigger") == n_planes
    assert call_names.count("cam.prepare_acquisition") == n_planes
    assert call_names.count("cam.start_acquisition") == n_planes
    assert call_names.count("cam.stop_acquisition") == n_planes
    assert call_names.count("cam.wait_for_frame") == n_planes
    
    # Verify snap_trigger was called with correct parameters
    snap_calls = [c for c in facade.calls if c[0] == "trig.snap_trigger"]
    assert len(snap_calls) == n_planes
    for call in snap_calls:
        assert call[2]["laser_pin"] == 3
        assert call[2]["camera_pin"] == 4
        assert call[2]["exposure_us"] == 100000


@pytest.mark.nohardware
def test_zstack_position_clamping():
    """Z positions are clamped to piezo range."""
    facade = build_mock_facade()
    facade.z_stage_con.pos_range_um = (10.0, 90.0)
    
    n_planes = 20
    mock_frames = np.random.randint(0, 1000, (n_planes, 64, 64), dtype=np.uint16)
    facade.cam.set_canned_data(mock_frames)
    
    params = ZStackParams(
        n_planes=n_planes,
        step_um=10.0,  # Would span 190 µm unclamped
        pulsed=False,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=False, z_start=50.0)
    
    # All positions should be within range
    assert all(10.0 <= z <= 90.0 for z in z_positions)
    assert z_positions[0] == 10.0  # Clamped to min
    assert z_positions[-1] == 90.0  # Clamped to max


@pytest.mark.nohardware
def test_zstack_autofocus_success():
    """Autofocus computes focus from gradient energy and moves stage."""
    facade = build_mock_facade()
    
    n_planes = 11
    # Create simple synthetic stack with clear peak at center
    # Use purely deterministic pattern (no random component)
    mock_frames = []
    for i in range(n_planes):
        # Simple triangular sharpness profile centered at plane 5
        sharpness = 1.0 - abs(i - 5) / 6.0
        sharpness = max(0.2, sharpness)  # Floor at 0.2
        
        # Create deterministic edge grid
        frame = np.ones((128, 128), dtype=np.float32) * 500
        # Grid pattern with strength proportional to sharpness
        grid_val = sharpness * 3000.0
        frame[::4, :] = grid_val
        frame[:, ::4] = grid_val
        
        frame = frame.astype(np.uint16)
        mock_frames.append(frame)
    
    facade.cam.set_canned_data(np.array(mock_frames))
    
    params = ZStackParams(
        n_planes=n_planes,
        step_um=1.0,
        pulsed=False,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run_autofocus(z_start=50.0)
    
    # Verify autofocus moved the stage
    call_names = facade.call_names()
    set_pos_calls = [c for c in facade.calls if c[0] == "z_stage_con.set_pos_um"]
    
    # Should have: initial set to z_start, n_planes moves, return to z_start, move to focus
    assert len(set_pos_calls) >= n_planes + 3
    
    # Final position should be within scanned range (since peak is at center)
    final_z = set_pos_calls[-1][1][0]
    assert min(z_positions) <= final_z <= max(z_positions)


@pytest.mark.nohardware
def test_zstack_autofocus_flat_profile_raises():
    """Autofocus raises if gradient profile is too flat for quadratic fit."""
    facade = build_mock_facade()
    
    n_planes = 7
    # Create perfectly flat/constant frames - no gradient variation
    mock_frames = []
    for i in range(n_planes):
        # All planes identical - flat gradient profile
        frame = np.ones((128, 128), dtype=np.uint16) * 1000
        mock_frames.append(frame)
    
    facade.cam.set_canned_data(np.array(mock_frames))
    
    params = ZStackParams(
        n_planes=n_planes,
        step_um=1.0,
        pulsed=False,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    # Should raise because gradient profile has no variation
    with pytest.raises(RuntimeError, match="Autofocus failed"):
        workflow.run_autofocus(z_start=50.0)


@pytest.mark.nohardware
def test_zstack_pulsed_without_trig_raises():
    """Hardware-triggered mode without trig facade raises RuntimeError."""
    facade = build_mock_facade()
    facade.trig = None  # Remove trigger facade
    
    params = ZStackParams(
        n_planes=5,
        step_um=1.0,
        pulsed=True,
        laser_pin=3,
        camera_pin=4,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    with pytest.raises(RuntimeError, match="trig facade is not configured"):
        workflow.run(save_stack=False)


@pytest.mark.nohardware
def test_zstack_pulsed_without_pins_raises():
    """Hardware-triggered mode without laser/camera pins raises ValueError."""
    facade = build_mock_facade()
    
    params = ZStackParams(
        n_planes=5,
        step_um=1.0,
        pulsed=True,
        laser_pin=None,  # Missing
        camera_pin=4,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    with pytest.raises(ValueError, match="pulsed=True requires laser_pin and camera_pin"):
        workflow.run(save_stack=False)


@pytest.mark.nohardware
def test_zstack_missing_z_stage_raises():
    """Workflow raises if z_stage_con is missing from facade."""
    facade = build_mock_facade()
    facade.z_stage_con = None
    
    params = ZStackParams(
        n_planes=5,
        step_um=1.0,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    with pytest.raises(RuntimeError, match="requires z_stage_con"):
        workflow.run(save_stack=False)


@pytest.mark.nohardware
def test_zstack_missing_cam_raises():
    """Workflow raises if cam is missing from facade."""
    facade = build_mock_facade()
    facade.cam = None
    
    params = ZStackParams(
        n_planes=5,
        step_um=1.0,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    with pytest.raises(RuntimeError, match="requires cam"):
        workflow.run(save_stack=False)


@pytest.mark.nohardware
def test_zstack_missing_laser_raises():
    """Workflow raises if laser_con is missing from facade."""
    facade = build_mock_facade()
    facade.laser_con = None
    
    params = ZStackParams(
        n_planes=5,
        step_um=1.0,
        measurements_root="/tmp",
    )
    
    workflow = ZStackWorkflow(facade, params)
    
    with pytest.raises(RuntimeError, match="requires laser_con"):
        workflow.run(save_stack=False)


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

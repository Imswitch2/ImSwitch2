"""Tests for DefocusScanWorkflow — headless defocus scan acquisition."""

import numpy as np
import pytest
from unittest.mock import MagicMock

from imswitch.imcontrol.model.workflows import (
    DefocusScanParams,
    DefocusScanWorkflow,
    RecordingParams,
    RecordingWorkflow,
    build_mock_facade,
)


@pytest.mark.nohardware
def test_defocus_scan_basic():
    """Basic defocus scan: correct number of z positions, recordings called."""
    facade = build_mock_facade()
    
    # Create a real RecordingParams and mock RecordingWorkflow
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
        move_waveplate=True,
        record_h=True,
        record_v=True,
        measurements_root=None,
        measurement_name_addition="",
    )
    
    # Mock RecordingWorkflow
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    # Configure defocus scan
    params = DefocusScanParams(
        n_z_planes=5,
        z_step_um=2.0,
        z_center_um=50.0,
        scramble=False,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    z_positions = workflow.run()
    
    # Verify correct number of z positions
    assert len(z_positions) == 5
    
    # Verify z positions are centered around 50.0 µm with 2.0 µm steps
    expected_positions = [46.0, 48.0, 50.0, 52.0, 54.0]
    np.testing.assert_allclose(z_positions, expected_positions, atol=0.01)
    
    # Verify recording.run() was called 5 times
    assert recording.run.call_count == 5
    
    # Verify method call sequence
    call_names = facade.call_names()
    assert "z_stage_con.activate_ext_control" in call_names
    assert call_names.count("z_stage_con.set_pos_um") == 5 + 1  # 5 planes + return to start
    
    # Verify measurement_name_addition was set correctly for each z position
    # (We can't directly verify this through the mock, but we can check that
    # the params object was modified)
    assert "_z" in recording.params.measurement_name_addition


@pytest.mark.nohardware
def test_defocus_scan_current_position():
    """Defocus scan with z_center_um=None uses current position."""
    facade = build_mock_facade()
    
    # Set current z position to 60.0 µm
    facade.z_stage_con._pos = 60.0
    
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    params = DefocusScanParams(
        n_z_planes=3,
        z_step_um=5.0,
        z_center_um=None,  # Should use current position
        scramble=False,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    z_positions = workflow.run()
    
    # Verify positions centered around 60.0 µm
    expected_positions = [55.0, 60.0, 65.0]
    np.testing.assert_allclose(z_positions, expected_positions, atol=0.01)


@pytest.mark.nohardware
def test_defocus_scan_position_clamping():
    """Z positions are clamped to piezo range."""
    facade = build_mock_facade()
    
    # MockZStageConFacade has pos_range_um = (0.0, 100.0)
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    # Request scan that would go below 0.0 µm
    params = DefocusScanParams(
        n_z_planes=5,
        z_step_um=5.0,
        z_center_um=5.0,  # Would result in positions: -5, 0, 5, 10, 15
        scramble=False,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    z_positions = workflow.run()
    
    # Verify first position is clamped to 0.0
    assert z_positions[0] == pytest.approx(0.0, abs=0.01)
    
    # Request scan that would go above 100.0 µm
    params2 = DefocusScanParams(
        n_z_planes=5,
        z_step_um=5.0,
        z_center_um=95.0,  # Would result in positions: 85, 90, 95, 100, 105
        scramble=False,
    )
    
    workflow2 = DefocusScanWorkflow(facade, recording, params2)
    z_positions2 = workflow2.run()
    
    # Verify last position is clamped to 100.0
    assert z_positions2[-1] == pytest.approx(100.0, abs=0.01)


@pytest.mark.nohardware
def test_defocus_scan_scramble_order():
    """Scramble option randomizes Z position order."""
    facade = build_mock_facade()
    
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    # Set random seed for reproducibility
    np.random.seed(42)
    
    params = DefocusScanParams(
        n_z_planes=10,
        z_step_um=1.0,
        z_center_um=50.0,
        scramble=True,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    z_positions = workflow.run()
    
    # Verify we got all 10 positions
    assert len(z_positions) == 10
    
    # Verify positions are NOT in sequential order
    expected_sequential = [45.5, 46.5, 47.5, 48.5, 49.5, 50.5, 51.5, 52.5, 53.5, 54.5]
    # They should contain the same values, but in different order
    np.testing.assert_allclose(sorted(z_positions), expected_sequential, atol=0.01)
    # But not in the same order
    assert z_positions != expected_sequential


@pytest.mark.nohardware
def test_defocus_scan_return_to_start():
    """Piezo returns to original position after scan."""
    facade = build_mock_facade()
    
    # Set starting position
    facade.z_stage_con._pos = 30.0
    
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    params = DefocusScanParams(
        n_z_planes=5,
        z_step_um=2.0,
        z_center_um=50.0,  # Far from start position
        scramble=False,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    workflow.run()
    
    # Verify last set_pos_um call returns to 30.0
    set_pos_calls = [c for c in facade.calls if c[0] == "z_stage_con.set_pos_um"]
    assert set_pos_calls[-1][1][0] == pytest.approx(30.0, abs=0.01)


@pytest.mark.nohardware
def test_defocus_scan_missing_z_stage():
    """Raises RuntimeError if z_stage_con is missing."""
    facade = build_mock_facade()
    facade.z_stage_con = None
    
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    recording.run = MagicMock()
    
    params = DefocusScanParams(
        n_z_planes=5,
        z_step_um=2.0,
        z_center_um=50.0,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    
    with pytest.raises(RuntimeError, match="z_stage_con"):
        workflow.run()


@pytest.mark.nohardware
def test_defocus_scan_measurement_name_additions():
    """Verify measurement_name_addition is set correctly for each z position."""
    facade = build_mock_facade()
    
    recording_params = RecordingParams(
        pin488=3,
        pin405=4,
        camerapin=5,
        start488=0,
        start405=0,
        start_camera=0,
        width488=1000,
        width405=1000,
        width_camera=50000,
        dwelltime=60000,
        delay_time=1000,
        frame_number=10,
        measurement_name_addition="",  # Start empty
    )
    
    recording = MagicMock(spec=RecordingWorkflow)
    recording.params = recording_params
    
    # Track what measurement_name_addition was set before each run() call
    name_additions = []
    
    def capture_name_addition():
        name_additions.append(recording.params.measurement_name_addition)
    
    recording.run = MagicMock(side_effect=capture_name_addition)
    
    params = DefocusScanParams(
        n_z_planes=3,
        z_step_um=1.0,
        z_center_um=50.0,
        scramble=False,
    )
    
    workflow = DefocusScanWorkflow(facade, recording, params)
    z_positions = workflow.run()
    
    # Verify we captured 3 name additions
    assert len(name_additions) == 3
    
    # Verify format: _z{z:.2f}um
    assert name_additions[0] == "_z49.00um"
    assert name_additions[1] == "_z50.00um"
    assert name_additions[2] == "_z51.00um"

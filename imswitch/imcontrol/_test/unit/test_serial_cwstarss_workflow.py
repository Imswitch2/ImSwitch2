"""Tests for SerialCWSTARSSWorkflow.

Verifies the serial power sweep: for each (488, 405) power combination from the
Cartesian product, the workflow moves to a spiral position and runs CWSTARSS
with the corresponding powers. Stage returns to initial position on exit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    CWSTARSSParams,
    CWSTARSSWorkflow,
    SerialCWSTARSSParams,
    SerialCWSTARSSWorkflow,
    build_mock_facade,
)

pytestmark = pytest.mark.nohardware


# ---------------------------------------------------------------------------
# Full workflow verification
# ---------------------------------------------------------------------------


def test_serial_cwstarss_cartesian_product_positions():
    """Verify total positions equals len(powers_488) × len(powers_405)."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,  # Will be overwritten
        power_405_mw=0.0,  # Will be overwritten
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    powers_488 = [10.0, 20.0, 30.0]
    powers_405 = [5.0, 15.0]
    expected_positions = len(powers_488) * len(powers_405)  # 3 × 2 = 6

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=powers_488,
        powers_405_mw=powers_405,
        fps=5.0,
        duration_s=0.01,
        step_units=1560.0,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Count stage moves (should be n_positions + 1 for return to start)
    move_calls = [call for call in facade.calls if call[0] == "stage_con.move_to"]
    assert len(move_calls) == expected_positions + 1  # 6 positions + 1 return


def test_serial_cwstarss_power_combinations_correct():
    """Verify each CWSTARSS run receives the correct (p488, p405) pair."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    # Track cwstarss.run() calls by monitoring params changes
    run_powers = []
    original_run = cwstarss.run

    def tracked_run():
        # Capture current powers before run
        run_powers.append((cwstarss.params.power_488_mw, cwstarss.params.power_405_mw))
        original_run()

    cwstarss.run = tracked_run

    powers_488 = [10.0, 20.0]
    powers_405 = [5.0, 15.0, 25.0]

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=powers_488,
        powers_405_mw=powers_405,
        fps=5.0,
        duration_s=0.01,
        step_units=1560.0,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Expected Cartesian product order
    expected_powers = [
        (10.0, 5.0),
        (10.0, 15.0),
        (10.0, 25.0),
        (20.0, 5.0),
        (20.0, 15.0),
        (20.0, 25.0),
    ]

    assert run_powers == expected_powers


def test_serial_cwstarss_stage_returns_to_start():
    """Verify stage returns to initial position after workflow completes."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    # Set initial stage position
    initial_x, initial_y = 1000.0, 2000.0
    facade.stage_con.move_to(initial_x, initial_y)

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0, 20.0],
        powers_405_mw=[5.0],
        fps=5.0,
        duration_s=0.01,
        step_units=1560.0,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Final stage position should be back at initial
    final_pos = facade.stage_con.get_position()
    assert final_pos == (initial_x, initial_y)


def test_serial_cwstarss_stage_returns_even_on_failure():
    """Verify stage returns to start even if workflow raises an exception."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    initial_x, initial_y = 500.0, 1500.0
    facade.stage_con.move_to(initial_x, initial_y)

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    # Make cwstarss.run() fail on second call
    call_count = [0]
    original_run = cwstarss.run

    def failing_run():
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("Simulated workflow failure")
        original_run()

    cwstarss.run = failing_run

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0, 20.0],
        powers_405_mw=[5.0],
        fps=5.0,
        duration_s=0.01,
        step_units=1560.0,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)

    # Workflow should propagate exception but still return stage to start
    with pytest.raises(RuntimeError, match="Simulated workflow failure"):
        workflow.run()

    # Stage should be back at initial position
    final_pos = facade.stage_con.get_position()
    assert final_pos == (initial_x, initial_y)


# ---------------------------------------------------------------------------
# Spiral positioning
# ---------------------------------------------------------------------------


def test_serial_cwstarss_spiral_positions_correct():
    """Verify stage moves follow spiral pattern with correct offsets."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    initial_x, initial_y = 1000.0, 2000.0
    facade.stage_con.move_to(initial_x, initial_y)

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    step_units = 1560.0
    powers_488 = [10.0]
    powers_405 = [5.0, 15.0, 25.0]  # 3 positions

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=powers_488,
        powers_405_mw=powers_405,
        fps=5.0,
        duration_s=0.01,
        step_units=step_units,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    
    # Record position before workflow starts
    calls_before = len([call for call in facade.calls if call[0] == "stage_con.move_to"])
    
    workflow.run()

    # Get all stage moves after workflow started
    move_calls = [call for call in facade.calls if call[0] == "stage_con.move_to"]
    # Skip the setup move(s) made before workflow.run()
    experiment_moves = move_calls[calls_before:]
    
    # First 3 moves are the experiment positions (last is return to start)
    positions = [call[1] for call in experiment_moves[:3]]

    # Expected spiral offsets: (0,0), (1,0), (1,1) scaled by step_units
    expected_positions = [
        (initial_x + 0 * step_units, initial_y + 0 * step_units),  # Center
        (initial_x + 1 * step_units, initial_y + 0 * step_units),  # Right
        (initial_x + 1 * step_units, initial_y + 1 * step_units),  # Up
    ]

    for actual, expected in zip(positions, expected_positions):
        assert abs(actual[0] - expected[0]) < 1e-6
        assert abs(actual[1] - expected[1]) < 1e-6


def test_serial_cwstarss_step_units_applied_correctly():
    """Verify step_units parameter scales spiral offsets correctly."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    initial_x, initial_y = 0.0, 0.0
    facade.stage_con.move_to(initial_x, initial_y)

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    step_units = 100.0
    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0],
        powers_405_mw=[5.0, 15.0],  # 2 positions
        fps=5.0,
        duration_s=0.01,
        step_units=step_units,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    
    # Record position before workflow starts
    calls_before = len([call for call in facade.calls if call[0] == "stage_con.move_to"])
    
    workflow.run()

    move_calls = [call for call in facade.calls if call[0] == "stage_con.move_to"]
    experiment_moves = move_calls[calls_before:]
    positions = [call[1] for call in experiment_moves[:2]]

    # Expected: (0,0) and (1*step_units, 0)
    assert positions[0] == (0.0, 0.0)
    assert positions[1] == (100.0, 0.0)


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_serial_cwstarss_empty_powers_488_raises():
    """Verify empty powers_488_mw raises ValueError."""
    facade = build_mock_facade()
    cwstarss = CWSTARSSWorkflow(
        facade,
        CWSTARSSParams(5.0, 0.01, 0.0, 0.0, Path("/tmp")),
    )

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[],  # Empty
        powers_405_mw=[5.0],
        fps=5.0,
        duration_s=0.01,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)

    with pytest.raises(ValueError, match="powers_488_mw must not be empty"):
        workflow.run()


def test_serial_cwstarss_empty_powers_405_raises():
    """Verify empty powers_405_mw raises ValueError."""
    facade = build_mock_facade()
    cwstarss = CWSTARSSWorkflow(
        facade,
        CWSTARSSParams(5.0, 0.01, 0.0, 0.0, Path("/tmp")),
    )

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0],
        powers_405_mw=[],  # Empty
        fps=5.0,
        duration_s=0.01,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)

    with pytest.raises(ValueError, match="powers_405_mw must not be empty"):
        workflow.run()


# ---------------------------------------------------------------------------
# Parameter propagation
# ---------------------------------------------------------------------------


def test_serial_cwstarss_propagates_fps_duration():
    """Verify fps and duration_s are propagated to CWSTARSS workflow."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=1.0,  # Will be overwritten
        duration_s=1.0,  # Will be overwritten
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    fps_target = 12.5
    duration_target = 3.5

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0],
        powers_405_mw=[5.0],
        fps=fps_target,
        duration_s=duration_target,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Verify cwstarss params were updated
    assert cwstarss.params.fps == fps_target
    assert cwstarss.params.duration_s == duration_target


def test_serial_cwstarss_propagates_measurements_root():
    """Verify measurements_root is propagated to CWSTARSS workflow."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp/old"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    new_root = Path("/tmp/new_measurements")
    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0],
        powers_405_mw=[5.0],
        fps=5.0,
        duration_s=0.01,
        measurements_root=new_root,
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Verify cwstarss params were updated
    assert cwstarss.params.measurements_root == new_root


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_serial_cwstarss_single_position():
    """Verify workflow works with single power combination (1 position)."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=[10.0],
        powers_405_mw=[5.0],
        fps=5.0,
        duration_s=0.01,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    # Should have 1 position + 1 return
    move_calls = [call for call in facade.calls if call[0] == "stage_con.move_to"]
    assert len(move_calls) == 2


def test_serial_cwstarss_large_cartesian_product():
    """Verify workflow handles large power combinations correctly."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    cwstarss_params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.001,  # Very short
        power_488_mw=0.0,
        power_405_mw=0.0,
        measurements_root=Path("/tmp"),
    )
    cwstarss = CWSTARSSWorkflow(facade, cwstarss_params)

    powers_488 = [10.0, 20.0, 30.0, 40.0, 50.0]
    powers_405 = [5.0, 10.0, 15.0, 20.0]
    expected_positions = 5 * 4  # 20

    serial_params = SerialCWSTARSSParams(
        powers_488_mw=powers_488,
        powers_405_mw=powers_405,
        fps=5.0,
        duration_s=0.001,
        measurements_root=Path("/tmp"),
    )

    workflow = SerialCWSTARSSWorkflow(facade, cwstarss, serial_params)
    workflow.run()

    move_calls = [call for call in facade.calls if call[0] == "stage_con.move_to"]
    assert len(move_calls) == expected_positions + 1  # 20 positions + 1 return

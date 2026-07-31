"""Tests for CWSTARSSWorkflow.

Verifies the photoselection experiment sequence: for each polarisation (H, V),
the workflow calls rotator positioning, pre-bleach, 488-only stream, and
488+405 stream in the correct order, with cleanup always called in finally.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    CWSTARSSParams,
    CWSTARSSWorkflow,
    build_mock_facade,
)

pytestmark = pytest.mark.nohardware


# ---------------------------------------------------------------------------
# Full workflow sequence
# ---------------------------------------------------------------------------


def test_cwstarss_calls_all_phases_for_both_polarisations(tmp_path):
    """Verify the full H→V sequence with all four phases per polarisation."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((10, 64, 64), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=10.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=tmp_path,
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Check that we have calls for both H and V polarisations
    call_names = facade.call_names()

    # H polarisation sequence
    assert "rotator_qwp.move_to_h" in call_names
    assert "rotator_hwp.move_to_h" in call_names

    # V polarisation sequence
    assert "rotator_qwp.move_to_v" in call_names
    assert "rotator_hwp.move_to_v" in call_names

    # Each polarisation should have 4 camera prepare/start/stop cycles:
    # 2 for streaming phases (488 only, 488+405)
    cam_prepare_count = sum(1 for name in call_names if name == "cam.prepare_acquisition")
    cam_start_count = sum(1 for name in call_names if name == "cam.start_acquisition")
    cam_stop_count = sum(1 for name in call_names if name == "cam.stop_acquisition")

    assert cam_prepare_count == 4  # 2 phases × 2 polarisations
    assert cam_start_count == 4
    assert cam_stop_count == 4

    # Cleanup should turn lasers off
    assert call_names.count("laser_con.laser_off") >= 2  # At least cleanup calls


def test_cwstarss_phase_order_correct():
    """Verify phases are called in the correct order for each polarisation."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    call_names = facade.call_names()

    # Find indices of key events for H polarisation
    h_rotator_idx = call_names.index("rotator_qwp.move_to_h")

    # Find first camera prepare after H rotator (should be for 488-only stream)
    first_cam_prepare = next(
        i for i in range(h_rotator_idx, len(call_names))
        if call_names[i] == "cam.prepare_acquisition"
    )

    # Find second camera prepare after H rotator (should be for 488+405 stream)
    second_cam_prepare = next(
        i for i in range(first_cam_prepare + 1, len(call_names))
        if call_names[i] == "cam.prepare_acquisition"
    )

    # Verify order: rotator → 488 stream → 488+405 stream
    assert h_rotator_idx < first_cam_prepare < second_cam_prepare

    # Same verification for V polarisation
    v_rotator_idx = call_names.index("rotator_qwp.move_to_v")
    assert v_rotator_idx > second_cam_prepare  # V comes after all H phases


def test_cwstarss_laser_powers_correct():
    """Verify correct laser powers are set during streaming phases."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((10, 32, 32), dtype=np.uint16))

    power_488 = 7.5
    power_405 = 3.2

    params = CWSTARSSParams(
        fps=10.0,
        duration_s=1.0,
        power_488_mw=power_488,
        power_405_mw=power_405,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Find all set_constant_power calls
    power_calls = [
        call for call in facade.calls
        if call[0] == "laser_con.set_constant_power"
    ]

    # Should have 6 total: 2 polarisations × (1 pre-bleach + 1×488 stream + 1×488+405 stream)
    assert len(power_calls) >= 4  # At least 2 pol × 2 streams (pre-bleach uses same power)

    # Check 488-only stream calls (names=["488"], powers=[power_488])
    single_laser_calls = [
        call for call in power_calls
        if call[1][0] == ["488"] and len(call[1][0]) == 1
    ]
    for call in single_laser_calls:
        assert call[1][1] == [power_488]

    # Check 488+405 stream calls (names=["488", "405"], powers=[power_488, power_405])
    dual_laser_calls = [
        call for call in power_calls
        if call[1][0] == ["488", "405"]
    ]
    for call in dual_laser_calls:
        assert call[1][1] == [power_488, power_405]


def test_cwstarss_uses_configured_laser_names():
    """Workflow should address facade laser keys from params, not fixed names."""
    facade = build_mock_facade()

    params = CWSTARSSParams(
        fps=10.0,
        duration_s=0.0,
        power_488_mw=7.5,
        power_405_mw=3.2,
        measurements_root=Path("/tmp"),
        laser_488_name="widefield",
        laser_405_name="activation",
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    power_calls = [
        call for call in facade.calls
        if call[0] == "laser_con.set_constant_power"
    ]
    off_calls = [
        call for call in facade.calls
        if call[0] == "laser_con.laser_off"
    ]

    assert any(call[1][0] == ["widefield"] for call in power_calls)
    assert any(call[1][0] == ["widefield", "activation"] for call in power_calls)
    assert all("488" not in call[1][0] and "405" not in call[1][0] for call in off_calls)


def test_cwstarss_n_frames_calculated_correctly():
    """Verify number of frames matches fps × duration_s."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((25, 32, 32), dtype=np.uint16))

    fps = 12.5
    duration = 2.0
    expected_n_frames = int(fps * duration)  # 25 frames

    params = CWSTARSSParams(
        fps=fps,
        duration_s=duration,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Check all prepare_acquisition calls
    prepare_calls = [
        call for call in facade.calls
        if call[0] == "cam.prepare_acquisition"
    ]

    # All should request the same number of frames
    for call in prepare_calls:
        assert call[1][0] == expected_n_frames


# ---------------------------------------------------------------------------
# Cleanup and error handling
# ---------------------------------------------------------------------------


def test_cwstarss_cleanup_called_on_success():
    """Verify cleanup is called even when workflow completes successfully."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Cleanup should call laser_off at the end
    call_names = facade.call_names()
    assert "laser_con.laser_off" in call_names


def test_cwstarss_cleanup_called_on_failure():
    """Verify cleanup is called even when a phase raises an exception."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)

    # Make rotator raise an exception
    original_move_to_h = facade.rotator_qwp.move_to_h

    def failing_move():
        original_move_to_h()
        raise RuntimeError("Rotator malfunction")

    facade.rotator_qwp.move_to_h = failing_move

    # Workflow should propagate the exception but still call cleanup
    with pytest.raises(RuntimeError, match="Rotator malfunction"):
        workflow.run()

    # Cleanup should still have been called (laser_off)
    call_names = facade.call_names()
    assert "laser_con.laser_off" in call_names


def test_cwstarss_cleanup_has_exception_handler():
    """Verify cleanup method has try/except to catch hardware failures.

    The cleanup method wraps laser_off in a try/except block to ensure
    hardware safety even if cleanup itself fails. This test verifies the
    exception handler exists by checking cleanup can be called with a
    failing laser controller without propagating the exception.
    """
    facade = build_mock_facade()
    params = CWSTARSSParams(
        fps=5.0,
        duration_s=0.01,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)

    # Make laser_off always fail
    def failing_laser_off(names):
        raise RuntimeError("Hardware communication error")

    facade.laser_con.laser_off = failing_laser_off

    # Call cleanup directly - should NOT propagate the exception
    workflow._cleanup()

    # If we get here, cleanup handled the exception correctly


# ---------------------------------------------------------------------------
# Polarisation and rotator control
# ---------------------------------------------------------------------------


def test_cwstarss_rotator_sequence_h_then_v():
    """Verify rotators move to H first, then V."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    call_names = facade.call_names()

    h_idx = call_names.index("rotator_qwp.move_to_h")
    v_idx = call_names.index("rotator_qwp.move_to_v")

    assert h_idx < v_idx  # H must come before V


def test_cwstarss_chained_rotator_moves():
    """Verify QWP and HWP both move via chained_move."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    call_names = facade.call_names()

    # For H: QWP.move_to_h should be immediately followed by HWP.move_to_h
    qwp_h_idx = call_names.index("rotator_qwp.move_to_h")
    hwp_h_idx = call_names.index("rotator_hwp.move_to_h")
    assert hwp_h_idx == qwp_h_idx + 1

    # For V: QWP.move_to_v should be immediately followed by HWP.move_to_v
    qwp_v_idx = call_names.index("rotator_qwp.move_to_v")
    hwp_v_idx = call_names.index("rotator_hwp.move_to_v")
    assert hwp_v_idx == qwp_v_idx + 1


# ---------------------------------------------------------------------------
# File saving
# ---------------------------------------------------------------------------


def test_cwstarss_saves_files_per_phase(tmp_path, monkeypatch):
    """Verify that TIFF files are saved for each phase/polarisation."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((5, 32, 32), dtype=np.uint16))

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=tmp_path,
    )

    # Mock time.strftime to get predictable filenames
    saved_files = []

    original_imwrite = __import__("tifffile").imwrite

    def mock_imwrite(filename, data):
        saved_files.append(Path(filename))
        # Don't actually write to avoid filesystem I/O in tests

    monkeypatch.setattr("tifffile.imwrite", mock_imwrite)

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Should have 4 files: 2 polarisations × 2 phases (488, 488+405)
    assert len(saved_files) == 4

    # Compare the phase labels exactly, with the _HHMMSS stamp stripped off.
    # Substring checks conflate the two: _save() appends time.strftime("%H%M%S"),
    # so a run at 12:34:05 names the 488-only file cwstarss_V_488_123405.tif and
    # a "405" not in name guard rejects it as if it were the 488+405 phase.
    labels = {re.sub(r"_\d{6}\.tif$", "", f.name) for f in saved_files}

    assert labels == {
        "cwstarss_H_488",
        "cwstarss_H_488_405",
        "cwstarss_V_488",
        "cwstarss_V_488_405",
    }


def test_cwstarss_save_uses_default_root_when_params_root_is_none(tmp_path, monkeypatch):
    """_save should accept measurements_root=None and use the workflow default."""
    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=None,
    )
    workflow = CWSTARSSWorkflow(build_mock_facade(), params)
    saved_files = []

    monkeypatch.setenv("IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT", str(tmp_path))
    monkeypatch.setattr("tifffile.imwrite", lambda filename, data: saved_files.append(Path(filename)))

    workflow._save(np.zeros((1, 8, 8), dtype=np.uint16), "test")

    assert len(saved_files) == 1
    assert saved_files[0].is_relative_to(tmp_path)


def test_cwstarss_handles_no_camera_data_gracefully():
    """Verify workflow handles None from get_data without crashing."""
    facade = build_mock_facade()
    # Do NOT set canned data → get_data returns None

    params = CWSTARSSParams(
        fps=5.0,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)

    # Should not crash even if camera returns no data
    workflow.run()

    # Workflow should complete and call cleanup
    call_names = facade.call_names()
    assert "laser_con.laser_off" in call_names


# ---------------------------------------------------------------------------
# Parameter validation edge cases
# ---------------------------------------------------------------------------


def test_cwstarss_with_fractional_frames():
    """Verify fractional fps×duration is rounded to int frames."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((13, 32, 32), dtype=np.uint16))

    # 12.7 fps × 1.0 s = 12.7 frames → should be 12 or 13
    params = CWSTARSSParams(
        fps=12.7,
        duration_s=1.0,
        power_488_mw=5.0,
        power_405_mw=2.0,
        measurements_root=Path("/tmp"),
    )

    workflow = CWSTARSSWorkflow(facade, params)
    workflow.run()

    # Check that prepare_acquisition was called with an integer
    prepare_calls = [
        call for call in facade.calls
        if call[0] == "cam.prepare_acquisition"
    ]

    for call in prepare_calls:
        n_frames = call[1][0]
        assert isinstance(n_frames, int)
        assert 12 <= n_frames <= 13  # int(12.7) = 12

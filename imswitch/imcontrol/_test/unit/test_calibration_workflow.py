"""Tests for CalibrationWorkflow — polarisation calibration and segmentation check."""

import csv
import tempfile
from pathlib import Path

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    CalibrationParams,
    CalibrationWorkflow,
    build_mock_facade,
)


def test_calibration_params_defaults():
    """CalibrationParams should have sensible defaults."""
    params = CalibrationParams()
    assert params.n_steps_qwp == 10
    assert params.n_steps_hwp == 10
    assert params.laser_power_488_mw == 5.0
    assert params.exposure_us == 50000
    assert params.pulsed is True


def test_polarisation_calibration_writes_csv():
    """run_polarisation_calibration should produce a CSV with correct row count."""
    facade = build_mock_facade()

    # Construct a known quad-pixel pattern:
    # Top-left (0°), Top-right (45°), Bottom-left (90°), Bottom-right (135°)
    quad_image = np.zeros((100, 100), dtype=np.uint16)
    quad_image[::2, ::2] = 100    # p_90
    quad_image[::2, 1::2] = 200   # p_135
    quad_image[1::2, ::2] = 300   # p_45
    quad_image[1::2, 1::2] = 400  # p_0

    # Stack for mock camera: wrap in 3D (batch, H, W)
    facade.cam.set_canned_data(quad_image[np.newaxis, :, :])

    params = CalibrationParams(
        n_steps_qwp=3,
        n_steps_hwp=2,
        laser_power_488_mw=10.0,
        exposure_us=50000,
        laser_pin=1,
        camera_pin=0,
        pulsed=True,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        wf = CalibrationWorkflow(facade, params)
        wf.run_polarisation_calibration(save_folder=tmpdir)

        csv_path = Path(tmpdir) / "pol_calibration_cam.csv"
        assert csv_path.exists(), f"CSV not created at {csv_path}"

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # Should have n_qwp × n_hwp rows
        assert len(rows) == 3 * 2, f"Expected 6 rows, got {len(rows)}"

        # Check first row quad-pixel means
        first = rows[0]
        assert "qwp_angle" in first
        assert "hwp_angle" in first
        assert "p_0" in first
        assert "p_45" in first
        assert "p_90" in first
        assert "p_135" in first
        assert "mean_intensity" in first

        # Verify quad-pixel extraction from our crafted image
        # p_0 = mean(img[1::2, 1::2]) → 400
        # p_45 = mean(img[1::2, ::2]) → 300
        # p_90 = mean(img[::2, ::2]) → 100
        # p_135 = mean(img[::2, 1::2]) → 200
        p_0 = float(first["p_0"])
        p_45 = float(first["p_45"])
        p_90 = float(first["p_90"])
        p_135 = float(first["p_135"])

        assert abs(p_0 - 400.0) < 1.0, f"p_0 should be ~400, got {p_0}"
        assert abs(p_45 - 300.0) < 1.0, f"p_45 should be ~300, got {p_45}"
        assert abs(p_90 - 100.0) < 1.0, f"p_90 should be ~100, got {p_90}"
        assert abs(p_135 - 200.0) < 1.0, f"p_135 should be ~200, got {p_135}"

        # Mean intensity should be (100+200+300+400)/4 = 250
        mean_int = float(first["mean_intensity"])
        assert abs(mean_int - 250.0) < 1.0, f"mean_intensity should be ~250, got {mean_int}"


def test_polarisation_calibration_rotator_calls():
    """run_polarisation_calibration should call move_abs with correct angles."""
    facade = build_mock_facade()

    # Simple 2x2 grid
    quad_image = np.ones((100, 100), dtype=np.uint16) * 50
    facade.cam.set_canned_data(quad_image[np.newaxis, :, :])

    params = CalibrationParams(
        n_steps_qwp=2,
        n_steps_hwp=2,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        wf = CalibrationWorkflow(facade, params)
        wf.run_polarisation_calibration(save_folder=tmpdir)

    # Check that rotators were moved
    call_names = facade.call_names()
    assert "rotator_hwp.move_abs" in call_names
    assert "rotator_qwp.move_abs" in call_names

    # Filter rotator calls
    hwp_calls = [c for c in facade.calls if c[0] == "rotator_hwp.move_abs"]
    qwp_calls = [c for c in facade.calls if c[0] == "rotator_qwp.move_abs"]

    # HWP should be moved 2 times (n_steps_hwp=2)
    assert len(hwp_calls) == 2, f"Expected 2 HWP moves, got {len(hwp_calls)}"

    # QWP should be moved 2 × 2 = 4 times (n_steps_hwp * n_steps_qwp)
    assert len(qwp_calls) == 4, f"Expected 4 QWP moves, got {len(qwp_calls)}"

    # Check angles: should be linspace(0, 180, 2) → [0.0, 180.0]
    hwp_angles = [c[1][0] for c in hwp_calls]
    assert 0.0 in hwp_angles
    assert 180.0 in hwp_angles


def test_polarisation_calibration_default_path():
    """run_polarisation_calibration without save_folder should create timestamped CSV."""
    facade = build_mock_facade()

    quad_image = np.ones((100, 100), dtype=np.uint16) * 50
    facade.cam.set_canned_data(quad_image[np.newaxis, :, :])

    with tempfile.TemporaryDirectory() as tmpdir:
        params = CalibrationParams(
            n_steps_qwp=2,
            n_steps_hwp=2,
            measurements_root=tmpdir,
        )

        wf = CalibrationWorkflow(facade, params)
        wf.run_polarisation_calibration(save_folder=None)

        # Should create folder YYYY_MM_DD/polcal_HHMMSS.csv
        date_folders = list(Path(tmpdir).glob("*"))
        assert len(date_folders) > 0, "No date folder created"

        csv_files = list(Path(tmpdir).rglob("polcal_*.csv"))
        assert len(csv_files) == 1, f"Expected 1 CSV file, found {len(csv_files)}"


def test_calibration_workflow_run_alias_writes_csv(tmp_path):
    """run() should execute the default polarisation calibration workflow."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.ones((1, 20, 20), dtype=np.uint16))

    params = CalibrationParams(n_steps_qwp=1, n_steps_hwp=1)
    wf = CalibrationWorkflow(facade, params)

    wf.run(save_folder=tmp_path)

    assert (tmp_path / "pol_calibration_cam.csv").exists()


def test_polarisation_calibration_uses_default_root_when_params_root_is_none(tmp_path, monkeypatch):
    """run_polarisation_calibration should accept measurements_root=None."""
    facade = build_mock_facade()
    facade.cam.set_canned_data(np.ones((1, 20, 20), dtype=np.uint16))

    monkeypatch.setattr(
        "imswitch.imcontrol.model.workflows.calibration.DEFAULT_MEASUREMENTS_ROOT",
        tmp_path,
    )

    params = CalibrationParams(n_steps_qwp=1, n_steps_hwp=1, measurements_root=None)
    wf = CalibrationWorkflow(facade, params)

    wf.run_polarisation_calibration()

    csv_files = list(tmp_path.rglob("polcal_*.csv"))
    assert len(csv_files) == 1


def test_segmentation_param_check_runs():
    """run_segmentation_param_check should acquire an image and log stats."""
    facade = build_mock_facade()

    test_image = np.random.randint(0, 1000, size=(100, 100), dtype=np.uint16)
    facade.cam.set_canned_data(test_image[np.newaxis, :, :])

    params = CalibrationParams()
    wf = CalibrationWorkflow(facade, params)

    # Should not raise
    wf.run_segmentation_param_check()

    # Check that camera was used
    call_names = facade.call_names()
    assert "cam.prepare_live" in call_names
    assert "cam.start_live" in call_names
    assert "cam.stop_live" in call_names
    assert "cam.get_data" in call_names


def test_snap_triggered_calls_correct_pins():
    """_snap_triggered should call snap_trigger with correct pin configuration."""
    facade = build_mock_facade()

    test_image = np.ones((100, 100), dtype=np.uint16) * 123
    facade.cam.set_canned_data(test_image[np.newaxis, :, :])

    params = CalibrationParams(
        laser_pin=5,
        camera_pin=3,
        exposure_us=100000,
    )

    wf = CalibrationWorkflow(facade, params)
    result = wf._snap_triggered(
        laser_pin=params.laser_pin,
        camera_pin=params.camera_pin,
        exposure_us=params.exposure_us,
    )

    # Check that trigger was called with correct kwargs
    trig_calls = [c for c in facade.calls if c[0] == "trig.snap_trigger"]
    assert len(trig_calls) == 1

    call_kwargs = trig_calls[0][2]
    assert call_kwargs["laser_pin"] == 5
    assert call_kwargs["camera_pin"] == 3
    assert call_kwargs["exposure_us"] == 100000

    # Check returned data
    assert isinstance(result, np.ndarray)
    assert result.shape == (100, 100)
    assert np.all(result == 123)


def test_snap_triggered_handles_no_data():
    """_snap_triggered should return zeros if camera returns None."""
    facade = build_mock_facade()

    # Set no canned data (get_data will return None)
    facade.cam.set_canned_data(None)

    params = CalibrationParams()
    wf = CalibrationWorkflow(facade, params)

    result = wf._snap_triggered(
        laser_pin=1,
        camera_pin=0,
        exposure_us=50000,
    )

    # Should return fallback zeros
    assert isinstance(result, np.ndarray)
    assert result.shape == (1804, 1804)
    assert result.dtype == np.uint16
    assert np.all(result == 0)


def test_calibration_workflow_requires_rotators():
    """run_polarisation_calibration should raise if rotators are None."""
    facade = build_mock_facade()
    facade.rotator_hwp = None

    params = CalibrationParams()
    wf = CalibrationWorkflow(facade, params)

    with pytest.raises(RuntimeError, match="HWP or QWP rotator facade is None"):
        wf.run_polarisation_calibration(save_folder="/tmp")


def test_calibration_workflow_requires_camera():
    """_snap_triggered should raise if camera facade is None."""
    facade = build_mock_facade()
    facade.cam = None

    params = CalibrationParams()
    wf = CalibrationWorkflow(facade, params)

    with pytest.raises(RuntimeError, match="camera facade is None"):
        wf._snap_triggered(laser_pin=1, camera_pin=0, exposure_us=50000)


def test_calibration_workflow_requires_trigger():
    """_snap_triggered should raise if trigger facade is None."""
    facade = build_mock_facade()
    facade.trig = None

    params = CalibrationParams()
    wf = CalibrationWorkflow(facade, params)

    with pytest.raises(RuntimeError, match="trigger facade is None"):
        wf._snap_triggered(laser_pin=1, camera_pin=0, exposure_us=50000)


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

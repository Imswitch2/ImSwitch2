"""Tests for the workflow facade and its mock variant.

The mock facade is the contract Phase-1 workflow ports depend on; these
tests pin its call-recording shape so a regression in the mock would
surface here rather than in a workflow test.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    MicroscopeFacade,
    RotatorPresets,
    build_mock_facade,
)


# ---------------------------------------------------------------------------
# MockMicroscopeFacade structural contract
# ---------------------------------------------------------------------------


def test_build_mock_facade_returns_populated_facade():
    f = build_mock_facade()
    assert isinstance(f, MicroscopeFacade)
    assert f.laser_con is not None
    assert f.cam is not None
    assert f.trig is not None
    assert f.stage_con is not None
    assert f.z_stage_con is not None
    assert f.rotator_hwp is not None
    assert f.rotator_qwp is not None
    assert f.calls == []


def test_laser_con_records_calls():
    f = build_mock_facade()
    f.laser_con.set_constant_power(["488"], [50.0])
    f.laser_con.set_triggered_mode(["488"], [25.0])
    f.laser_con.set_modulation_mode(["488"])
    f.laser_con.laser_off(["488"])

    assert f.call_names() == [
        "laser_con.set_constant_power",
        "laser_con.set_triggered_mode",
        "laser_con.set_modulation_mode",
        "laser_con.laser_off",
    ]
    assert f.calls[0] == ("laser_con.set_constant_power", (["488"], [50.0]), {})


def test_set_modulation_mode_accepts_none():
    f = build_mock_facade()
    f.laser_con.set_modulation_mode(None)
    assert f.calls == [("laser_con.set_modulation_mode", (None,), {})]


def test_cam_get_data_returns_canned_array():
    f = build_mock_facade()
    arr = np.zeros((4, 8, 8), dtype=np.uint16)
    f.cam.set_canned_data(arr)

    f.cam.prepare_acquisition(4)
    f.cam.start_acquisition()
    result = f.cam.get_data()
    f.cam.stop_acquisition()

    assert result is arr
    assert f.call_names() == [
        "cam.prepare_acquisition",
        "cam.start_acquisition",
        "cam.get_data",
        "cam.stop_acquisition",
    ]


def test_cam_wait_for_frame_true_when_canned():
    f = build_mock_facade()
    f.cam.set_canned_data(np.zeros((1, 2, 2), dtype=np.uint16))
    assert f.cam.wait_for_frame(timeout_s=0.01) is True


def test_cam_wait_for_frame_false_when_empty():
    f = build_mock_facade()
    assert f.cam.wait_for_frame(timeout_s=0.01) is False


def test_cam_expo_is_mutable():
    f = build_mock_facade()
    f.cam.expo = 10000.0
    assert f.cam.expo == 10000.0


def test_trig_snap_trigger_records_kwargs():
    f = build_mock_facade()
    f.trig.snap_trigger(laser_pin=8, camera_pin=11, exposure_us=5000)
    assert f.calls == [
        ("trig.snap_trigger", (), {"laser_pin": 8, "camera_pin": 11, "exposure_us": 5000}),
    ]


def test_stage_con_move_and_position():
    f = build_mock_facade()
    f.stage_con.move_to(1560.0, 0.0)
    assert f.stage_con.get_position() == (1560.0, 0.0)
    assert f.call_names() == ["stage_con.move_to", "stage_con.get_position"]


def test_z_stage_con_default_range_and_round_trip():
    f = build_mock_facade()
    assert f.z_stage_con.pos_range_um == (0.0, 100.0)
    f.z_stage_con.activate_ext_control()
    f.z_stage_con.set_pos_um(42.0)
    assert f.z_stage_con.read_pos_um() == 42.0


# ---------------------------------------------------------------------------
# Rotator H/V + chained moves
# ---------------------------------------------------------------------------


def test_rotator_chained_move_calls_followup():
    f = build_mock_facade()
    f.rotator_qwp.chained_move_to_h(f.rotator_hwp.move_to_h)
    f.rotator_qwp.chained_move_to_v(f.rotator_hwp.move_to_v)

    assert f.call_names() == [
        "rotator_qwp.move_to_h",
        "rotator_hwp.move_to_h",
        "rotator_qwp.move_to_v",
        "rotator_hwp.move_to_v",
    ]


def test_rotator_presets_drive_position():
    from imswitch.imcontrol.model.workflows.mock_facade import _MockRotator, _Recorder
    rec = _Recorder()
    rot = _MockRotator(rec, "rotator_test", RotatorPresets(h_deg=12.5, v_deg=102.5))
    rot.move_to_h()
    assert rot.position() == 12.5
    rot.move_to_v()
    assert rot.position() == 102.5


# ---------------------------------------------------------------------------
# Real-adapter input validation (no hardware needed)
# ---------------------------------------------------------------------------


def test_laser_con_validates_name_power_length():
    from imswitch.imcontrol.model.workflows.facade import LaserConFacade

    facade = LaserConFacade({"488": object(), "405": object()})
    with pytest.raises(ValueError, match="length mismatch"):
        facade.set_constant_power(["488"], [10.0, 20.0])


def test_laser_con_unknown_name_raises():
    from imswitch.imcontrol.model.workflows.facade import LaserConFacade

    facade = LaserConFacade({"488": object()})
    with pytest.raises(KeyError, match="561"):
        facade.laser_off(["561"])


def test_trig_facade_snap_without_pulsegen_raises():
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    trig = TrigFacade(pulsegen=None)
    assert trig.connected is False
    with pytest.raises(RuntimeError, match="no pulse generator"):
        trig.snap_trigger(laser_pin=1, camera_pin=2, exposure_us=100)

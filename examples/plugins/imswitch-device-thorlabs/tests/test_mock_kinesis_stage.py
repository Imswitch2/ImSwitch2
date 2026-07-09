"""The extracted Kinesis stage manager runs hardware-free via its mock driver."""

from imswitch.pluginapi import PositionerInfo
from imswitch_device_thorlabs.positioners import KinesisStageManager


def _info(**props):
    base = {"snr": "MOCK_MLS203", "useMock": True}
    base.update(props)
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName="thorlabs.kinesis-stage",
        managerProperties=base,
        axes=["X", "Y"],
        forPositioning=True,
        forScanning=False,
    )


def test_mock_stage_instantiates_and_reports_axes():
    mgr = KinesisStageManager(_info(), "XY")
    try:
        assert list(mgr.axes) == ["X", "Y"]
        assert mgr.position == {"X": 0.0, "Y": 0.0}
    finally:
        mgr.finalize()


def test_mock_stage_absolute_and_relative_moves_track_position():
    mgr = KinesisStageManager(_info(), "XY")
    try:
        mgr.setPosition(1.5, "X")
        mgr.move(0.5, "X")
        mgr.setPosition(-2.0, "Y")
        assert mgr.position["X"] == 2.0
        assert mgr.position["Y"] == -2.0
    finally:
        mgr.finalize()


def test_driver_units_scale_positions():
    # 1000 driver units per position unit: commanding 2.0 stores 2.0 back.
    mgr = KinesisStageManager(_info(driverUnitsPerPositionUnit=1000.0), "XY")
    try:
        mgr.setPosition(2.0, "X")
        assert mgr.position["X"] == 2.0
    finally:
        mgr.finalize()


def test_jog_start_stop_is_safe_and_updates_position():
    mgr = KinesisStageManager(_info(), "XY")
    try:
        mgr.jog_start("X", +1)
        mgr.jog_stop("X")
        assert mgr.position["X"] == 0.0
    finally:
        mgr.finalize()


def test_home_on_init_resets_axes():
    mgr = KinesisStageManager(_info(homeOnInit=True), "XY")
    try:
        assert mgr.position == {"X": 0.0, "Y": 0.0}
    finally:
        mgr.finalize()


def test_unknown_axis_raises():
    import pytest

    mgr = KinesisStageManager(_info(), "XY")
    try:
        with pytest.raises(ValueError):
            mgr.setPosition(1.0, "Z")
    finally:
        mgr.finalize()

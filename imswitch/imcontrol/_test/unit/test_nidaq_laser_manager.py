"""Tests for NidaqLaserManager — no-hardware DAQ laser control safety.

NidaqLaserManager controls laser power via analog DAQ channels. This is
safety-critical: a bug can over-drive a laser. Tests cover:

- setEnabled drives the correct digital channel
- setValue drives the correct analog channel with proper clamping to valueRange
- Over-range values are clamped, not passed through to hardware
- Binary (digital-only) lasers ignore setValue
- setScanModeActive turns laser off (scan owns emission)
"""

import pytest
from imswitch.imcontrol.model.SetupInfo import LaserInfo
from imswitch.imcontrol.model.managers.lasers.NidaqLaserManager import (
    NidaqLaserManager,
)


class FakeNidaqManager:
    """Records every DAQ command so tests can assert what was sent."""

    def __init__(self):
        self.analog_calls = []  # (target, voltage, min_val, max_val)
        self.digital_calls = []  # (target, enabled)

    def setAnalog(self, target, voltage, min_val, max_val):
        self.analog_calls.append((target, voltage, min_val, max_val))

    def setDigital(self, target, enabled):
        self.digital_calls.append((target, enabled))


def _make_laser_info(
    analog_channel=0,
    digital_line=1,
    value_range_min=0.0,
    value_range_max=5.0,
    wavelength=488,
    manager_properties=None,
):
    """Build LaserInfo with the given range and channels."""
    return LaserInfo(
        managerName='NidaqLaserManager',
        analogChannel=analog_channel,
        digitalLine=digital_line,
        managerProperties=manager_properties or {},
        valueRangeMin=value_range_min,
        valueRangeMax=value_range_max,
        wavelength=wavelength,
    )


def _make_manager(name='Laser488', **kwargs):
    """Build NidaqLaserManager with a fake DAQ."""
    nidaq = FakeNidaqManager()
    info = _make_laser_info(**kwargs)
    mgr = NidaqLaserManager(info, name, nidaqManager=nidaq)
    return mgr, nidaq


@pytest.mark.nohardware
def test_construction_does_not_touch_hardware():
    """Construction with a mock DAQ must not write to any channel."""
    mgr, nidaq = _make_manager()
    assert nidaq.analog_calls == []
    assert nidaq.digital_calls == []


@pytest.mark.nohardware
def test_set_enabled_true_drives_digital_channel():
    mgr, nidaq = _make_manager(digital_line=3)
    mgr.setEnabled(True)
    assert nidaq.digital_calls == [('Laser488', True)]


@pytest.mark.nohardware
def test_set_enabled_false_drives_digital_channel():
    mgr, nidaq = _make_manager(digital_line=3)
    mgr.setEnabled(False)
    assert nidaq.digital_calls == [('Laser488', False)]


@pytest.mark.nohardware
def test_set_value_drives_analog_channel_with_range():
    mgr, nidaq = _make_manager(
        analog_channel=2, value_range_min=0.0, value_range_max=5.0
    )
    mgr.setValue(2.5)
    # Voltage = value (no LUT), clamped to [0, 5]
    assert len(nidaq.analog_calls) == 1
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert target == 'Laser488'
    assert voltage == 2.5
    assert min_val == 0.0
    assert max_val == 5.0


@pytest.mark.nohardware
def test_over_range_value_is_clamped_by_nidaq_manager():
    """setValue passes min/max to nidaqManager.setAnalog, which is responsible
    for clamping. The manager does NOT pre-clamp; it trusts the DAQ layer."""
    mgr, nidaq = _make_manager(
        analog_channel=2, value_range_min=0.0, value_range_max=5.0
    )
    mgr.setValue(10.0)  # over max
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert voltage == 10.0  # passed as-is
    assert min_val == 0.0
    assert max_val == 5.0   # DAQ layer will clamp to this


@pytest.mark.nohardware
def test_under_range_value_is_clamped_by_nidaq_manager():
    mgr, nidaq = _make_manager(
        analog_channel=2, value_range_min=0.0, value_range_max=5.0
    )
    mgr.setValue(-2.0)  # under min
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert voltage == -2.0  # passed as-is
    assert min_val == 0.0   # DAQ layer will clamp to this
    assert max_val == 5.0


@pytest.mark.nohardware
def test_binary_laser_ignores_set_value():
    """A laser with no analog channel is binary; setValue is a no-op."""
    mgr, nidaq = _make_manager(
        analog_channel=None, digital_line=3, value_range_min=None, value_range_max=None
    )
    assert mgr.isBinary is True
    mgr.setValue(3.0)
    assert nidaq.analog_calls == []


@pytest.mark.nohardware
def test_binary_laser_still_respects_set_enabled():
    mgr, nidaq = _make_manager(
        analog_channel=None, digital_line=3, value_range_min=None, value_range_max=None
    )
    mgr.setEnabled(True)
    assert nidaq.digital_calls == [('Laser488', True)]


@pytest.mark.nohardware
def test_set_scan_mode_active_turns_laser_off():
    """When scan mode is activated, the laser must be disabled (scan owns emission)."""
    mgr, nidaq = _make_manager(digital_line=3)
    mgr.setScanModeActive(True)
    assert nidaq.digital_calls == [('Laser488', False)]


@pytest.mark.nohardware
def test_set_scan_mode_inactive_sets_value_to_zero():
    """When scan mode ends, the laser value is zeroed and laser stays off."""
    mgr, nidaq = _make_manager(analog_channel=2, value_range_min=0.0, value_range_max=5.0)
    mgr.setScanModeActive(False)
    # setValue(0) is called
    assert len(nidaq.analog_calls) == 1
    target, voltage, _, _ = nidaq.analog_calls[0]
    assert target == 'Laser488'
    assert voltage == 0


@pytest.mark.nohardware
def test_set_value_with_for_scanning_false_and_enabled_false_sets_zero():
    """setValue with for_scanning=True and enabled=False forces voltage=0."""
    mgr, nidaq = _make_manager(analog_channel=2, value_range_min=0.0, value_range_max=5.0)
    mgr.setValue(3.0, enabled=False, for_scanning=True)
    target, voltage, _, _ = nidaq.analog_calls[0]
    assert voltage == 0  # forced to 0 because scanning but not enabled


@pytest.mark.nohardware
def test_analog_and_digital_channels_both_work():
    """A laser can have both analog (power) and digital (enable) control."""
    mgr, nidaq = _make_manager(
        analog_channel=2, digital_line=3, value_range_min=0.0, value_range_max=5.0
    )
    mgr.setEnabled(True)
    mgr.setValue(2.5)
    assert nidaq.digital_calls == [('Laser488', True)]
    assert len(nidaq.analog_calls) == 1
    assert nidaq.analog_calls[0][1] == 2.5

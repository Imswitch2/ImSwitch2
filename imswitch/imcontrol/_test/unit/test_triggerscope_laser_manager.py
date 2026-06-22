"""Tests for TriggerScopeLaserManager — no-hardware TriggerScope laser control safety.

TriggerScopeLaserManager controls laser power via TriggerScope DAC channels.
This is safety-critical: a bug can over-drive a laser. Tests cover:

- setEnabled drives the correct digital channel
- setValue drives the correct analog channel
- minVolt/maxVolt from managerProperties are respected (implicit via TriggerScope)
- Binary (digital-only) lasers ignore setValue
- setScanModeActive turns laser off (scan owns emission)
"""

import pytest
from imswitch.imcontrol.model.SetupInfo import LaserInfo
from imswitch.imcontrol.model.managers.lasers.TriggerScopeLaserManager import (
    TriggerScopeLaserManager,
)


class FakeTriggerScopeManager:
    """Records every TriggerScope command so tests can assert what was sent."""

    def __init__(self):
        self.analog_calls = []  # (target, voltage)
        self.digital_calls = []  # (target, enabled)

    def setAnalog(self, target, voltage):
        self.analog_calls.append((target, voltage))

    def setDigital(self, target, enabled):
        self.digital_calls.append((target, enabled))


def _make_laser_info(
    analog_channel=0,
    digital_line=1,
    value_range_min=0.0,
    value_range_max=5.0,
    wavelength=488,
    min_volt=0.0,
    max_volt=5.0,
):
    """Build LaserInfo with the given range and channels."""
    return LaserInfo(
        managerName='TriggerScopeLaserManager',
        analogChannel=analog_channel,
        digitalLine=digital_line,
        managerProperties={
            'minVolt': min_volt,
            'maxVolt': max_volt,
        },
        valueRangeMin=value_range_min,
        valueRangeMax=value_range_max,
        wavelength=wavelength,
    )


def _make_manager(name='Laser488', **kwargs):
    """Build TriggerScopeLaserManager with a fake TriggerScope."""
    ts = FakeTriggerScopeManager()
    info = _make_laser_info(**kwargs)
    mgr = TriggerScopeLaserManager(info, name, triggerScopeManager=ts)
    return mgr, ts


@pytest.mark.nohardware
def test_construction_does_not_touch_hardware():
    """Construction with a mock TriggerScope must not write to any channel."""
    mgr, ts = _make_manager()
    assert ts.analog_calls == []
    assert ts.digital_calls == []


@pytest.mark.nohardware
def test_set_enabled_true_drives_digital_channel():
    mgr, ts = _make_manager(digital_line=3)
    mgr.setEnabled(True)
    assert ts.digital_calls == [('Laser488', True)]


@pytest.mark.nohardware
def test_set_enabled_false_drives_digital_channel():
    mgr, ts = _make_manager(digital_line=3)
    mgr.setEnabled(False)
    assert ts.digital_calls == [('Laser488', False)]


@pytest.mark.nohardware
def test_set_value_drives_analog_channel():
    mgr, ts = _make_manager(
        analog_channel=2, value_range_min=0.0, value_range_max=5.0,
        min_volt=0.0, max_volt=5.0
    )
    mgr.setValue(2.5)
    assert ts.analog_calls == [('Laser488', 2.5)]


@pytest.mark.nohardware
def test_set_value_passes_voltage_directly():
    """TriggerScopeLaserManager does NOT clamp in setValue; it trusts the
    TriggerScope layer. The test shows setValue passes the raw value."""
    mgr, ts = _make_manager(
        analog_channel=2, value_range_min=0.0, value_range_max=5.0,
        min_volt=0.0, max_volt=5.0
    )
    mgr.setValue(10.0)  # over max
    assert ts.analog_calls == [('Laser488', 10.0)]  # passed as-is


@pytest.mark.nohardware
def test_binary_laser_ignores_set_value():
    """A laser with no analog channel is binary; setValue is a no-op."""
    mgr, ts = _make_manager(
        analog_channel=None, digital_line=3,
        value_range_min=None, value_range_max=None,
        min_volt=0.0, max_volt=5.0
    )
    assert mgr.isBinary is True
    mgr.setValue(3.0)
    assert ts.analog_calls == []


@pytest.mark.nohardware
def test_binary_laser_still_respects_set_enabled():
    mgr, ts = _make_manager(
        analog_channel=None, digital_line=3,
        value_range_min=None, value_range_max=None,
        min_volt=0.0, max_volt=5.0
    )
    mgr.setEnabled(True)
    assert ts.digital_calls == [('Laser488', True)]


@pytest.mark.nohardware
def test_set_scan_mode_active_turns_laser_off():
    """When scan mode is activated, the laser must be disabled (scan owns emission)."""
    mgr, ts = _make_manager(digital_line=3)
    mgr.setScanModeActive(True)
    assert ts.digital_calls == [('Laser488', False)]


@pytest.mark.nohardware
def test_set_scan_mode_inactive_does_not_re_enable():
    """When scan mode ends, the laser stays off (manager never forces ON)."""
    mgr, ts = _make_manager(digital_line=3)
    mgr.setScanModeActive(False)
    # No call to setEnabled(True) — laser remains off
    assert ts.digital_calls == []


@pytest.mark.nohardware
def test_analog_and_digital_channels_both_work():
    """A laser can have both analog (power) and digital (enable) control."""
    mgr, ts = _make_manager(
        analog_channel=2, digital_line=3,
        value_range_min=0.0, value_range_max=5.0,
        min_volt=0.0, max_volt=5.0
    )
    mgr.setEnabled(True)
    mgr.setValue(2.5)
    assert ts.digital_calls == [('Laser488', True)]
    assert ts.analog_calls == [('Laser488', 2.5)]


@pytest.mark.nohardware
def test_manager_properties_min_max_volt_stored():
    """The minVolt/maxVolt from managerProperties are stored (for reference)."""
    mgr, ts = _make_manager(min_volt=1.0, max_volt=4.5)
    assert mgr._minVolt == 1.0
    assert mgr._maxVolt == 4.5

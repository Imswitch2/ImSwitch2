"""Tests for NidaqPositionerManager — no-hardware DAQ positioner control safety.

NidaqPositionerManager controls stage position via analog DAQ channels. This is
safety-critical: a bug can crash a stage. Tests cover:

- setPosition writes the correct channel with proper voltage conversion
- min/max voltage limits are passed to the DAQ layer for clamping
- Position tracking updates correctly
- move uses relative displacement from tracked position
- Only supports single-axis positioners (multi-axis construction fails)
"""

import pytest
from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.NidaqPositionerManager import (
    NidaqPositionerManager,
)


class FakeNidaqManager:
    """Records every DAQ command so tests can assert what was sent."""

    def __init__(self):
        self.analog_calls = []  # (target, voltage, min_val, max_val)

    def setAnalog(self, target, voltage, min_val, max_val):
        self.analog_calls.append((target, voltage, min_val, max_val))


def _make_positioner_info(
    axes=None,
    conversion_factor=1.0,
    min_volt=0.0,
    max_volt=10.0,
):
    """Build PositionerInfo with the given properties."""
    if axes is None:
        axes = ['Z']
    return PositionerInfo(
        managerName='NidaqPositionerManager',
        analogChannel=0,
        digitalLine=None,
        managerProperties={
            'conversionFactor': conversion_factor,
            'minVolt': min_volt,
            'maxVolt': max_volt,
        },
        axes=axes,
        forScanning=True,
    )


def _make_manager(name='NidaqZ', **kwargs):
    """Build NidaqPositionerManager with a fake DAQ."""
    nidaq = FakeNidaqManager()
    info = _make_positioner_info(**kwargs)
    mgr = NidaqPositionerManager(info, name, nidaqManager=nidaq)
    return mgr, nidaq


@pytest.mark.nohardware
def test_construction_does_not_touch_hardware():
    """Construction with a mock DAQ must not write to any channel."""
    mgr, nidaq = _make_manager()
    assert nidaq.analog_calls == []


@pytest.mark.nohardware
def test_initial_position_is_zero():
    mgr, nidaq = _make_manager(axes=['Z'])
    assert mgr.position['Z'] == 0.0


@pytest.mark.nohardware
def test_set_position_writes_voltage_with_conversion():
    """position is divided by conversionFactor to get voltage."""
    mgr, nidaq = _make_manager(
        axes=['Z'], conversion_factor=2.0, min_volt=0.0, max_volt=10.0
    )
    mgr.setPosition(6.0, 'Z')  # 6 / 2 = 3 V
    assert len(nidaq.analog_calls) == 1
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert target == 'NidaqZ'
    assert voltage == 3.0
    assert min_val == 0.0
    assert max_val == 10.0


@pytest.mark.nohardware
def test_set_position_updates_tracked_position():
    mgr, nidaq = _make_manager(axes=['Z'], conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')
    assert mgr.position['Z'] == 6.0


@pytest.mark.nohardware
def test_move_adds_to_current_position():
    mgr, nidaq = _make_manager(axes=['Z'], conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')  # 3 V
    mgr.move(4.0, 'Z')          # 6 + 4 = 10 -> 5 V
    assert mgr.position['Z'] == 10.0
    assert len(nidaq.analog_calls) == 2
    assert nidaq.analog_calls[1][1] == 5.0  # voltage


@pytest.mark.nohardware
def test_min_max_volt_passed_to_daq_layer():
    """The DAQ layer is responsible for clamping to minVolt/maxVolt."""
    mgr, nidaq = _make_manager(
        axes=['Z'], conversion_factor=1.0, min_volt=1.0, max_volt=9.0
    )
    mgr.setPosition(5.0, 'Z')  # 5 V, within [1, 9]
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert min_val == 1.0
    assert max_val == 9.0


@pytest.mark.nohardware
def test_over_range_position_passed_to_daq_for_clamping():
    """NidaqPositionerManager does NOT pre-clamp; it trusts the DAQ layer."""
    mgr, nidaq = _make_manager(
        axes=['Z'], conversion_factor=1.0, min_volt=0.0, max_volt=10.0
    )
    mgr.setPosition(20.0, 'Z')  # 20 V, over max
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert voltage == 20.0  # passed as-is
    assert max_val == 10.0  # DAQ layer will clamp


@pytest.mark.nohardware
def test_get_abs_returns_tracked_position():
    mgr, nidaq = _make_manager(axes=['Z'])
    mgr.setPosition(6.0, 'Z')
    assert mgr.get_abs('Z') == 6.0


@pytest.mark.nohardware
def test_get_abs_raises_for_invalid_axis():
    mgr, nidaq = _make_manager(axes=['Z'])
    with pytest.raises(ValueError, match='Axis X not available'):
        mgr.get_abs('X')


@pytest.mark.nohardware
def test_reset_to_current_re_commands_current_position():
    """resetToCurrent re-sends the current position to the DAQ."""
    mgr, nidaq = _make_manager(axes=['Z'], conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')  # 3 V
    nidaq.analog_calls.clear()
    mgr.resetToCurrent()
    assert len(nidaq.analog_calls) == 1
    assert nidaq.analog_calls[0][1] == 3.0  # 6 / 2


@pytest.mark.nohardware
def test_multi_axis_construction_raises_runtime_error():
    """NidaqPositionerManager only supports one axis."""
    nidaq = FakeNidaqManager()
    info = _make_positioner_info(axes=['X', 'Y', 'Z'])
    with pytest.raises(RuntimeError, match='only supports one axis'):
        NidaqPositionerManager(info, 'NidaqXYZ', nidaqManager=nidaq)


@pytest.mark.nohardware
def test_negative_position_writes_negative_voltage():
    """Negative positions are valid and result in negative voltages."""
    mgr, nidaq = _make_manager(
        axes=['Z'], conversion_factor=2.0, min_volt=-10.0, max_volt=10.0
    )
    mgr.setPosition(-4.0, 'Z')  # -2 V
    target, voltage, min_val, max_val = nidaq.analog_calls[0]
    assert voltage == -2.0
    assert min_val == -10.0
    assert max_val == 10.0

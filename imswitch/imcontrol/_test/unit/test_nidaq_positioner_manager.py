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
from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.NidaqPositionerManager import (
    NidaqPositionerManager,
)


class FakeNidaqManager:
    """Records every DAQ command so tests can assert what was sent."""

    def __init__(self, fail=False, return_false=False, simulating=False):
        self.analog_calls = []  # (target, voltage, min_val, max_val)
        self.raise_on_error_calls = []
        self.fail = fail
        self.return_false = return_false
        self.isSimulating = simulating

    def setAnalog(self, target, voltage, min_val, max_val, *, raise_on_error=False):
        self.analog_calls.append((target, voltage, min_val, max_val))
        self.raise_on_error_calls.append(bool(raise_on_error))
        if self.fail:
            raise RuntimeError('analog write failed')
        if self.return_false:
            return False


@pytest.fixture(autouse=True)
def _isolated_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(dirtools.UserFileDirs, 'Config', str(tmp_path))
    return tmp_path


def _make_positioner_info(
    axes=None,
    conversion_factor=1.0,
    min_volt=0.0,
    max_volt=10.0,
    default_reference_voltage=None,
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
            **(
                {'defaultReferenceVoltage': default_reference_voltage}
                if default_reference_voltage is not None else {}
            ),
        },
        axes=axes,
        forScanning=True,
    )


def _make_manager(
    name='NidaqZ', *, fail=False, return_false=False, simulating=False, **kwargs
):
    """Build NidaqPositionerManager with a fake DAQ."""
    nidaq = FakeNidaqManager(
        fail=fail, return_false=return_false, simulating=simulating
    )
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
def test_position_persists_across_restart_without_moving():
    mgr, nidaq = _make_manager(conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')

    mgr2, nidaq2 = _make_manager(conversion_factor=2.0)

    assert nidaq.analog_calls == [('NidaqZ', 3.0, 0.0, 10.0)]
    assert nidaq2.analog_calls == []
    assert mgr2.position['Z'] == 6.0
    assert mgr2.isPositionRestored('Z') is True
    assert mgr2.isAxisReferenced('Z') is False


@pytest.mark.nohardware
def test_command_after_restore_clears_restored_provenance():
    mgr, _ = _make_manager(conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')

    mgr2, _ = _make_manager(conversion_factor=2.0)
    assert mgr2.isPositionRestored('Z') is True

    mgr2.setPosition(8.0, 'Z')

    assert mgr2.position['Z'] == 8.0
    assert mgr2.isPositionRestored('Z') is False


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

@pytest.mark.nohardware
def test_simulated_nidaq_positioner_starts_reference_safe_without_motion():
    mgr, nidaq = _make_manager(
        simulating=True,
        default_reference_voltage=5.0,
    )

    assert nidaq.analog_calls == []
    assert mgr.isAxisReferenced('Z') is True
    assert mgr.isReferenced is True


@pytest.mark.nohardware
def test_open_loop_positioner_starts_unreferenced():
    mgr, _ = _make_manager(default_reference_voltage=5.0)
    assert mgr.requiresReference is True
    assert mgr.isAxisReferenced('Z') is False
    assert mgr.isReferenced is False


@pytest.mark.nohardware
def test_reference_uses_configured_default_voltage():
    mgr, nidaq = _make_manager(
        conversion_factor=10.0,
        min_volt=0.0,
        max_volt=10.0,
        default_reference_voltage=5.0,
    )

    mgr.reference('Z')

    assert nidaq.analog_calls[-1] == ('NidaqZ', 5.0, 0.0, 10.0)
    assert nidaq.raise_on_error_calls[-1] is True
    assert mgr.position['Z'] == 50.0
    assert mgr.isAxisReferenced('Z') is True


@pytest.mark.nohardware
def test_reference_accepts_explicit_position():
    mgr, nidaq = _make_manager(
        conversion_factor=10.0,
        min_volt=0.0,
        max_volt=10.0,
        default_reference_voltage=5.0,
    )

    mgr.reference('Z', position=47.0)

    assert nidaq.analog_calls[-1] == ('NidaqZ', 4.7, 0.0, 10.0)
    assert mgr.position['Z'] == 47.0
    assert mgr.isAxisReferenced('Z') is True


@pytest.mark.nohardware
def test_reference_without_configured_default_is_rejected_without_motion():
    mgr, nidaq = _make_manager()

    with pytest.raises(ValueError, match='No defaultReferenceVoltage'):
        mgr.reference('Z')

    assert nidaq.analog_calls == []
    assert mgr.isAxisReferenced('Z') is False


@pytest.mark.nohardware
def test_reference_outside_voltage_range_is_rejected_without_motion():
    mgr, nidaq = _make_manager(
        conversion_factor=10.0,
        min_volt=0.0,
        max_volt=10.0,
        default_reference_voltage=5.0,
    )

    with pytest.raises(ValueError, match='outside'):
        mgr.reference('Z', position=-1.0)

    assert nidaq.analog_calls == []
    assert mgr.isAxisReferenced('Z') is False


@pytest.mark.nohardware
def test_silent_daq_write_failure_does_not_advance_tracked_or_persisted_position():
    mgr, _ = _make_manager(conversion_factor=2.0)
    mgr.setPosition(6.0, 'Z')

    failed, _ = _make_manager(
        conversion_factor=2.0,
        return_false=True,
    )
    assert failed.position['Z'] == 6.0

    failed.setPosition(8.0, 'Z')
    assert failed.position['Z'] == 6.0

    reopened, _ = _make_manager(conversion_factor=2.0)
    assert reopened.position['Z'] == 6.0


@pytest.mark.nohardware
def test_reference_write_failure_does_not_update_position_or_reference_state():
    mgr, nidaq = _make_manager(
        fail=True,
        conversion_factor=10.0,
        default_reference_voltage=5.0,
    )

    with pytest.raises(RuntimeError, match='analog write failed'):
        mgr.reference('Z')

    assert nidaq.raise_on_error_calls == [True]
    assert mgr.position['Z'] == 0.0
    assert mgr.isAxisReferenced('Z') is False


@pytest.mark.nohardware
def test_default_reference_voltage_must_be_inside_configured_range():
    with pytest.raises(ValueError, match='defaultReferenceVoltage'):
        _make_manager(
            min_volt=0.0,
            max_volt=10.0,
            default_reference_voltage=-1.0,
        )


"""No-hardware safety tests for MPBLaserManager.

The fake implements the small MPB command surface used by the manager and
tracks physical output separately from the desired ImSwitch setpoint. Tests
therefore assert command ordering as well as the final beam state.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.SetupInfo import LaserInfo
from imswitch.imcontrol.model.managers.lasers.MPBLaserManager import (
    MPBLaserManager,
)


class FakeMPB:
    def __init__(self, *, mode=1, output=0.0, limits=(100, 3100)):
        self.mode = int(mode)
        self.enabled = output > 0
        self.output = float(output)
        self.setpoint = max(float(output), float(limits[0]))
        self.limits = tuple(limits)
        self.commands = []
        self.fail_commands = set()
        self.empty_commands = set()

    def query(self, command):
        self.commands.append(command)
        if command in self.fail_commands:
            raise RuntimeError(f'simulated failure for {command}')
        if command in self.empty_commands:
            return None

        if command == 'GETSN':
            return 'S >TEST-MPB'
        if command == 'GETPOWERENABLE':
            return f'D > {self.mode} '
        if command == 'GETPOWERSETPTLIM 0':
            return f'F >{self.limits[0]} {self.limits[1]}'
        if command == 'POWER 0':
            value = self.output if self.enabled else 0.0
            return f'F >{value}'
        if command.startswith('POWERENABLE '):
            self.mode = int(command.rsplit(' ', 1)[-1])
            return f'D >{self.mode}'
        if command.startswith('SETPOWER 0 '):
            self.setpoint = float(command.rsplit(' ', 1)[-1])
            if self.enabled:
                self.output = self.setpoint
            return f'F >{self.setpoint}'
        if command == 'SETLDENABLE 0':
            self.enabled = False
            self.output = 0.0
            return 'D >0'
        if command == 'SETLDENABLE 1':
            self.enabled = True
            self.output = self.setpoint
            return 'D >1'
        raise AssertionError(f'Unexpected fake MPB command: {command}')


def _laser_info(**property_overrides):
    properties = {
        'rs232device': 'mpb',
        'rampDownEnabled': True,
        'rampDownDurationS': 0,
        'rampDownSteps': 3,
        'rampDownDwellS': 0,
        'useMockOnFailure': True,
    }
    properties.update(property_overrides)
    return LaserInfo(
        managerName='MPBLaserManager',
        managerProperties=properties,
        wavelength=775,
        valueRangeMin=0,
        valueRangeMax=3100,
        valueRangeStep=1,
    )


def _manager(fake, **property_overrides):
    rs232s = SimpleNamespace(_subManagers={'mpb': fake})
    return MPBLaserManager(
        _laser_info(**property_overrides),
        'MPB775',
        rs232sManager=rs232s,
    )


@pytest.mark.nohardware
def test_startup_recovers_live_apc_output_with_ramp_before_off():
    fake = FakeMPB(mode=1, output=1000, limits=(100, 3100))

    manager = _manager(fake)

    assert fake.commands == [
        'GETPOWERENABLE',
        'GETPOWERSETPTLIM 0',
        'POWER 0',
        'SETPOWER 0 700',
        'SETPOWER 0 400',
        'SETPOWER 0 100',
        'SETLDENABLE 0',
        'GETSN',
    ]
    assert fake.enabled is False
    assert manager._enabled is False


@pytest.mark.nohardware
def test_startup_apc_laser_already_dark_skips_power_writes():
    fake = FakeMPB(mode=1, output=0, limits=(100, 3100))

    manager = _manager(fake)

    assert fake.commands[-3:] == ['POWER 0', 'SETLDENABLE 0', 'GETSN']
    assert not any(command.startswith('SETPOWER') for command in fake.commands)
    assert manager._enabled is False


@pytest.mark.nohardware
def test_startup_mode_correction_never_reenables_emission():
    fake = FakeMPB(mode=0, output=1000, limits=(100, 3100))

    manager = _manager(fake)

    assert fake.commands == [
        'GETPOWERENABLE',
        'SETLDENABLE 0',
        'POWERENABLE 1',
        'GETPOWERENABLE',
        'GETPOWERSETPTLIM 0',
        'GETSN',
    ]
    assert 'SETLDENABLE 1' not in fake.commands
    assert fake.mode == 1
    assert fake.enabled is False
    assert manager._enabled is False


@pytest.mark.nohardware
def test_startup_failure_attempts_immediate_off_before_mock_fallback():
    fake = FakeMPB(mode=1, output=1000)
    fake.fail_commands.add('GETPOWERSETPTLIM 0')

    manager = _manager(fake)

    assert manager._isMock is True
    assert fake.commands[-1] == 'SETLDENABLE 0'
    assert fake.enabled is False
    assert 'SETLDENABLE 1' not in fake.commands


@pytest.mark.nohardware
def test_strict_startup_failure_raises_after_immediate_off():
    fake = FakeMPB(mode=1, output=1000)
    fake.fail_commands.add('GETPOWERSETPTLIM 0')

    with pytest.raises(RuntimeError, match='initialization failed'):
        _manager(fake, useMockOnFailure=False)

    assert fake.commands[-1] == 'SETLDENABLE 0'
    assert fake.enabled is False


@pytest.mark.nohardware
def test_power_is_cached_while_off_and_flushed_before_enable():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    fake.commands.clear()

    manager.setValue(1200)
    assert fake.commands == []
    assert manager._desired_power == 1200

    manager.setEnabled(True)
    assert fake.commands == ['SETPOWER 0 1200', 'SETLDENABLE 1']
    assert fake.enabled is True
    assert fake.output == 1200
    assert manager._enabled is True


@pytest.mark.nohardware
def test_normal_off_ramps_but_retains_desired_power_for_next_enable():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    manager.setEnabled(False)

    assert fake.commands == [
        'POWER 0',
        'SETPOWER 0 700',
        'SETPOWER 0 400',
        'SETPOWER 0 100',
        'SETLDENABLE 0',
    ]
    assert manager._desired_power == 1000
    assert manager._enabled is False
    assert fake.setpoint == 100

    fake.commands.clear()
    manager.setEnabled(True)
    assert fake.commands == ['SETPOWER 0 1000', 'SETLDENABLE 1']
    assert fake.output == 1000


@pytest.mark.nohardware
def test_zero_power_request_ramps_down_and_prevents_reenable():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    manager.setValue(0)

    assert fake.commands[-1] == 'SETLDENABLE 0'
    assert any(command.startswith('SETPOWER 0 ') for command in fake.commands)
    assert manager._desired_power == 0
    assert fake.enabled is False

    fake.commands.clear()
    assert manager.setEnabled(True) is False
    assert fake.commands == ['SETLDENABLE 0']
    assert fake.enabled is False


@pytest.mark.nohardware
def test_failed_ramp_falls_back_to_immediate_off(caplog):
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()
    fake.fail_commands.add('SETPOWER 0 700')

    manager.setEnabled(False)

    assert fake.commands == [
        'POWER 0',
        'SETPOWER 0 700',
        'SETLDENABLE 0',
    ]
    assert fake.enabled is False
    assert manager._enabled is False
    assert 'Falling back to immediate OFF' in caplog.text


@pytest.mark.nohardware
def test_empty_off_acknowledgement_leaves_state_unknown():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.empty_commands.add('SETLDENABLE 0')

    with pytest.raises(RuntimeError, match='returned no acknowledgement'):
        manager.emergencyDisable(reason='test missing acknowledgement')

    assert manager._enabled is None


@pytest.mark.nohardware
def test_ramp_can_be_disabled_but_normal_off_still_disables():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake, rampDownEnabled=False)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    manager.setEnabled(False)

    assert fake.commands == ['SETLDENABLE 0']
    assert fake.enabled is False


@pytest.mark.nohardware
def test_emergency_disable_never_queries_or_ramps_power():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    manager.emergencyDisable(reason='test interlock')

    assert fake.commands == ['SETLDENABLE 0']
    assert fake.enabled is False


@pytest.mark.nohardware
def test_positive_power_change_while_enabled_is_applied_immediately():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    manager.setValue(1400)

    assert fake.commands == ['SETPOWER 0 1400']
    assert manager._desired_power == 1400
    assert fake.output == 1400


@pytest.mark.nohardware
def test_finalize_uses_safe_disable_and_reports_immediate_off_failure():
    fake = FakeMPB(mode=1, output=0)
    manager = _manager(fake)
    manager.setValue(1000)
    manager.setEnabled(True)
    fake.commands.clear()

    assert manager.finalize() is True
    assert fake.commands[-1] == 'SETLDENABLE 0'
    assert fake.enabled is False

    fake.fail_commands.add('SETLDENABLE 0')
    assert manager.finalize() is False

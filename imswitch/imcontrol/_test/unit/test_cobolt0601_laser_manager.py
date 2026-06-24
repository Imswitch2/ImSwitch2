"""Tests for legacy Cobolt0601LaserManager scan-power safety."""

import logging

import pytest

from imswitch.imcontrol.model.managers.lasers.Cobolt0601LaserManager import (
    Cobolt0601LaserManager,
)


class FakeLegacyCobolt:
    def __init__(self):
        self.enabled = False
        self.digital_mod = False
        self.mode = 'APC'
        self.power_sp = 0.0
        self.power_mod = 0.0
        self.queries = []
        self.enter_mod_mode_calls = 0

    def enter_mod_mode(self):
        self.enter_mod_mode_calls += 1

    def query(self, text):
        self.queries.append(text)
        return '0'


def _build_manager(num_lasers=1):
    manager = object.__new__(Cobolt0601LaserManager)
    manager._laser = FakeLegacyCobolt()
    manager._numLasers = num_lasers
    manager._digitalMod = False
    manager._enabled = False
    manager._setpoint_mw = 0.0
    manager._Cobolt0601LaserManager__logger = logging.getLogger(
        'test.Cobolt0601LaserManager'
    )
    return manager


@pytest.mark.nohardware
def test_zero_value_clears_stale_power_before_scan_mode():
    manager = _build_manager()
    manager._laser.power_sp = 35.0

    manager.setValue(0)

    assert manager._setpoint_mw == 0
    assert manager._laser.power_sp == 0
    assert manager._laser.mode == 'ACC'
    assert manager._laser.queries[-2:] == ['ci', 'slc 0.0']

    manager.setScanModeActive(True)

    assert manager._laser.power_mod == 0
    assert manager._laser.digital_mod is True
    assert manager._laser.enabled is False


@pytest.mark.nohardware
def test_scan_mode_uses_cached_gui_setpoint_not_hardware_power_query():
    manager = _build_manager(num_lasers=2)
    manager.setValue(80)
    manager._laser.power_sp = 999.0

    manager.setScanModeActive(True)

    assert manager._laser.power_mod == 40.0

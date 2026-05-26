"""Tests for Cobolt0601NewLaserManager — autodetect, safe-state, fail-closed.

The manager is exercised through a fake ``send_cmd``-driven laser that
records every command and lets the test choose whether SCPI variants are
"supported" by the simulated firmware. This is the same shape as the
``MockCobolt06`` shipped under ``lantzdrivers_mock/`` — the explicit fake
here keeps each test self-contained.

Tests cover:
- legacy-firmware autodetection (default)
- SCPI-firmware autodetection
- safe-state startup command sequence (both firmwares)
- setEnabled(True) wire trace + ``_enabled`` only updates on success
- setEnabled(False) wire trace + critical ``l0`` failure keeps ``_enabled = True``
- setValue cached while off; flushed while on
- setScanModeActive enters modulation; falls back to enable state on deactivate
- SCPI modulation-power unit (Watts) — pins the conversion deliberately
"""

import pytest

from imswitch.imcontrol.model.managers.lasers.Cobolt0601NewLaserManager import (
    Cobolt0601NewLaserManager,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeLaser:
    """Records every send_cmd call. Replies based on `firmware` attribute."""

    def __init__(self, firmware: str = 'legacy', failed_cmds: set = None):
        self.firmware = firmware                  # 'legacy' or 'scpi'
        self.cmds: list = []
        self.failed_cmds: set = set(failed_cmds or ())
        # Used by test_critical_off_failure to simulate a stuck controller.

    def send_cmd(self, command: str) -> str:
        self.cmds.append(command)
        if command in self.failed_cmds:
            return 'Syntax error: illegal command'
        cl = command.lower().strip()
        is_scpi = cl.startswith(('laser:', 'las:'))
        if is_scpi and self.firmware != 'scpi':
            return 'Syntax error: illegal command'
        if cl == 'laser:runmode?':
            return 'ConstantPower'
        if cl == 'laser:power:setpoint?':
            return '0.005'
        if cl.endswith('?'):
            return '0'
        return 'OK'


def _build_manager(laser: FakeLaser, modulation_power_mw: float = 5.0):
    """Build a manager bound to ``laser`` without running the real __init__."""
    m = object.__new__(Cobolt0601NewLaserManager)
    m._laser = laser
    m._port = 'COM_TEST'
    m._modulation_power_mw = float(modulation_power_mw)
    m._setpoint_mw = 0
    m._enabled = False
    m._real_hw = True
    m._scpi = None
    # Real manager uses name-mangled logger; tests don't need its output.
    import logging
    m._Cobolt0601NewLaserManager__logger = logging.getLogger(
        'test.Cobolt0601NewLaserManager'
    )
    return m


# ---------------------------------------------------------------------------
# Firmware autodetection
# ---------------------------------------------------------------------------


def test_detect_firmware_legacy():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is False
    # Both probes were attempted
    assert 'LASer:RUNMode?' in laser.cmds
    assert 'LASer:POWer:SETPoint?' in laser.cmds


def test_detect_firmware_scpi():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is True


def test_detect_firmware_partial_scpi_treated_as_legacy():
    """If one probe succeeds but the other doesn't, fall back to legacy."""
    laser = FakeLaser(firmware='scpi', failed_cmds={'LASer:POWer:SETPoint?'})
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is False


# ---------------------------------------------------------------------------
# Safe-state init
# ---------------------------------------------------------------------------


def test_init_safe_state_legacy_command_sequence():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser, modulation_power_mw=5.0)
    m._scpi = False
    m._init_safe_state()

    # Legacy safe state: autostart off, set modulation power, enter mod
    # mode, enable digital gating, master off.
    assert laser.cmds == ['@cobas 0', 'slmp 5.0', 'em', 'sdmes 1', 'l0']
    assert m._enabled is False


def test_init_safe_state_scpi_command_sequence():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, modulation_power_mw=5.0)
    m._scpi = True
    m._init_safe_state()

    # SCPI safe state: same flow, SCPI command names. Modulation power is
    # expressed in Watts (5 mW → 0.005).
    assert laser.cmds == [
        '@cobas 0',
        'LASer:PowerModulation:POWer:SETPoint 0.005',
        'LAS:RUNM PowerModulation',
        'las:pm:dig:ena 1',
        'l0',
    ]
    assert m._enabled is False


# ---------------------------------------------------------------------------
# setEnabled — fail-closed
# ---------------------------------------------------------------------------


def test_set_enabled_true_legacy_command_sequence():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert laser.cmds == ['p 0.050000', 'cp', 'l1']
    assert m._enabled is True


def test_set_enabled_true_scpi_command_sequence():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._scpi = True
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert laser.cmds == ['p 0.050000', 'LAS:RUNM ConstantPower', 'l1']
    assert m._enabled is True


def test_set_enabled_true_fails_closed_when_l1_rejected():
    """If l1 fails, _enabled must NOT become True, and the manager
    drives the laser back to the modulation-mode safe state."""
    laser = FakeLaser(firmware='legacy', failed_cmds={'l1'})
    m = _build_manager(laser)
    m._scpi = False
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert m._enabled is False
    # After the failure we expect the off-recovery sequence to follow.
    assert 'l0' in laser.cmds


def test_set_enabled_true_fails_closed_when_cp_rejected():
    """If the mode-entry command is rejected, _enabled stays False."""
    laser = FakeLaser(firmware='legacy', failed_cmds={'cp'})
    m = _build_manager(laser)
    m._scpi = False
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert m._enabled is False


def test_set_enabled_false_critical_l0_failure_keeps_enabled_true():
    """If the master-off l0 fails on disable, the manager must NOT mark
    the laser as off — the operator needs to know the transition did not
    actually take effect."""
    laser = FakeLaser(firmware='legacy', failed_cmds={'l0'})
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = True
    m.setEnabled(False)

    assert m._enabled is True   # state preserved because l0 rejected


def test_set_enabled_false_normal_path_returns_to_safe_state():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = True
    m.setEnabled(False)

    # Master off first, then modulation-mode safe state.
    assert laser.cmds[0] == 'l0'
    assert 'em' in laser.cmds
    assert m._enabled is False


# ---------------------------------------------------------------------------
# setValue — gated on _enabled
# ---------------------------------------------------------------------------


def test_set_value_while_off_is_cached_not_sent():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = False
    m.setValue(75)

    assert m._setpoint_mw == 75
    assert laser.cmds == []     # nothing on the wire


def test_set_value_while_on_is_flushed_to_hardware():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = True
    m.setValue(80)

    assert m._setpoint_mw == 80
    assert laser.cmds == ['p 0.080000']


def test_set_value_ignores_non_numeric():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = True
    m.setValue('not-a-number')
    assert laser.cmds == []


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------


def test_scan_mode_active_enters_modulation_at_setpoint_power():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser, modulation_power_mw=5.0)
    m._scpi = False
    m._setpoint_mw = 100   # GUI setpoint
    m.setScanModeActive(True)

    # Modulation power uses the GUI setpoint when it's non-zero.
    assert 'slmp 100.0' in laser.cmds
    assert 'em' in laser.cmds
    assert 'sdmes 1' in laser.cmds
    assert 'l1' in laser.cmds


def test_scan_mode_inactive_returns_to_enable_state():
    """Leaving scan mode delegates to setEnabled(self._enabled)."""
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = False
    m.setScanModeActive(False)

    # Should have driven back to safe-off state (l0 + modulation safe state).
    assert 'l0' in laser.cmds


# ---------------------------------------------------------------------------
# SCPI modulation-power unit pin (Watts)
# ---------------------------------------------------------------------------


def test_scpi_set_modulation_power_uses_watts():
    """SCPI ``LASer:PowerModulation:POWer:SETPoint`` is assumed to take
    Watts (matching the SCPI convention used by ``LASer:POWer:SETPoint``).
    This test pins that behaviour so a future change is deliberate.

    If a firmware revision is found that expects milliwatts on this
    command, fix ``_enter_modulation_mode``/``setModulationPower`` AND
    update this test.
    """
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._scpi = True
    m.setModulationPower(5)  # 5 mW

    # 5 mW = 0.005 W
    assert 'LASer:PowerModulation:POWer:SETPoint 0.005' in laser.cmds


def test_legacy_set_modulation_power_uses_milliwatts():
    """Legacy ``slmp`` takes mW directly — pin that too."""
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m.setModulationPower(5)

    assert 'slmp 5.0' in laser.cmds


# ---------------------------------------------------------------------------
# Mock send_cmd contract
# ---------------------------------------------------------------------------


def test_mock_cobolt06_send_cmd_records_and_rejects_scpi_by_default():
    from imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601 import (
        MockCobolt06,
    )
    mock = MockCobolt06('COM_TEST')
    assert mock.send_cmd('l0') == 'OK'
    assert 'illegal command' in mock.send_cmd('LASer:RUNMode?').lower()
    assert mock.cmds == ['l0', 'LASer:RUNMode?']


def test_mock_cobolt06_send_cmd_scpi_mode_accepts_scpi():
    from imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601 import (
        MockCobolt06,
    )
    mock = MockCobolt06('COM_TEST')
    mock.firmware = 'scpi'
    assert mock.send_cmd('LASer:RUNMode?') == 'ConstantPower'

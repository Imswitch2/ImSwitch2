"""Tests for Cobolt0601NewLaserManager — autodetect, safe-state, fail-closed.

The manager is exercised through a fake ``send_cmd``-driven laser that
records every command and lets the test choose whether SCPI variants are
"supported" by the simulated firmware. This is the same shape as the
``MockCobolt06`` shipped under ``lantzdrivers_mock/`` — the explicit fake
here keeps each test self-contained.

Tests cover:
- firmware/model/serial identity readout (gfv?/sn?/glm?)
- legacy-firmware autodetection (default)
- SCPI-firmware autodetection, including upstream pycobolt's
  LASer:CP:POWer:SETPoint? probe
- safe-state startup command sequence (both firmwares)
- setEnabled(True) wire trace + ``_enabled`` only updates on success
- setEnabled(False) wire trace + critical ``l0`` failure keeps ``_enabled = True``
- setValue cached while off; flushed while on
- setScanModeActive enters modulation; falls back to enable state on deactivate
- SCPI modulation-power unit (mW by default, matching upstream pycobolt)
"""

import logging

import pytest

from imswitch.imcontrol.model.managers.lasers.Cobolt0601NewLaserManager import (
    Cobolt0601NewLaserManager,
)
from imswitch.imcontrol.model.managers.lasers._cobolt_protocol import (
    classify_reply,
    send_command,
)
from imswitch.imcontrol.model.managers.lasers._protocol import (
    CommandRejected,
    CommandTimeout,
    DeviceInitializationError,
    TransportFailure,
    UnexpectedReply,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeLaser:
    """Records every send_cmd call. Replies based on `firmware` attribute.

    Three independent failure channels, because the manager must tell them
    apart:

    - ``failed_cmds``  -> an explicit vendor rejection (command did NOT run)
    - ``raise_cmds``   -> a transport exception (outcome UNKNOWN)
    - ``empty_cmds``   -> an empty reply (outcome UNKNOWN)
    """

    def __init__(self, firmware: str = 'legacy', failed_cmds: set = None,
                 raise_cmds: dict = None, empty_cmds: set = None):
        self.firmware = firmware                  # 'legacy' or 'scpi'
        self.cmds: list = []
        self.failed_cmds: set = set(failed_cmds or ())
        self.raise_cmds: dict = dict(raise_cmds or {})
        self.empty_cmds: set = set(empty_cmds or ())
        self.serialnumber = 'SN12345'
        self.modelnumber = '0561-06-01-0100-C'
        self.firmware_version = '1.2.3.4'
        self.disconnected = False
        # Used by test_critical_off_failure to simulate a stuck controller.

    def disconnect(self):
        self.disconnected = True

    def send_cmd(self, command: str) -> str:
        self.cmds.append(command)
        if command in self.raise_cmds:
            raise self.raise_cmds[command]
        if command in self.empty_cmds:
            return ''
        if command in self.failed_cmds:
            return 'Syntax error: illegal command'
        cl = command.lower().strip()
        is_scpi = cl.startswith(('laser:', 'las:'))
        if cl == 'gfv?':
            return self.firmware_version
        if cl in ('sn?', 'gsn?'):
            return self.serialnumber
        if cl == 'glm?':
            return self.modelnumber
        if is_scpi and self.firmware != 'scpi':
            return 'Syntax error: illegal command'
        if cl == 'laser:runmode?':
            return 'ConstantPower'
        if cl == 'laser:cp:power:setpoint?':
            return '50.0'
        if cl == 'laser:power:setpoint?':
            # Current upstream pycobolt uses the CP-specific query instead.
            return 'Syntax error: illegal command'
        if cl.startswith('laser:power:setpoint '):
            return 'Syntax error: illegal command'
        if cl == 'laser:powermodulation:power:setpoint?':
            return '5.0'
        if cl.endswith('?'):
            return '0'
        return 'OK'


def _build_manager(laser: FakeLaser, modulation_power_mw: float = 5.0,
                   pause_mode: bool = False):
    """Build a manager bound to ``laser`` without running the real __init__."""
    m = object.__new__(Cobolt0601NewLaserManager)
    m._laser = laser
    m._port = 'COM_TEST'
    m._modulation_power_mw = float(modulation_power_mw)
    m._setpoint_mw = 0
    m._enabled = False
    m._real_hw = True
    m._scpi = None
    m._pause_mode = pause_mode
    m._emission_control = 'pause' if pause_mode else 'master'
    m._scpi_power_unit = 'mw'
    m._firmware_version = None
    m._serial_number = None
    m._model_number = None
    m._command_variant_cache = {}
    m._last_failure = None
    m._simulation = False
    # Real manager uses name-mangled logger; tests don't need its output.
    import logging
    m._Cobolt0601NewLaserManager__logger = logging.getLogger(
        'test.Cobolt0601NewLaserManager'
    )
    return m


# ---------------------------------------------------------------------------
# Firmware autodetection
# ---------------------------------------------------------------------------


def test_detect_firmware_records_identity():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._detect_firmware()

    assert m.getFirmwareInfo() == {
        'firmware': '1.2.3.4',
        'serial': 'SN12345',
        'model': '0561-06-01-0100-C',
        'commandSet': 'SCPI',
        'requestedEmissionControl': 'master',
        'resolvedEmissionControl': 'master',
        'emissionControl': 'master',
        'scpiPowerUnit': 'mW',
    }
    assert laser.cmds[:3] == ['gfv?', 'sn?', 'glm?']


def test_detect_firmware_legacy():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is False
    # Both probes were attempted
    assert 'LASer:RUNMode?' in laser.cmds
    assert 'LASer:POWer:SETPoint?' in laser.cmds


def test_detect_firmware_legacy_suppresses_expected_probe_warnings(caplog):
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)

    with caplog.at_level(logging.WARNING, logger='test.Cobolt0601NewLaserManager'):
        m._detect_firmware()

    assert caplog.records == []


def test_detect_firmware_scpi():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is True


def test_detect_firmware_scpi_accepts_cp_power_probe_without_generic_probe():
    """Current upstream pycobolt uses LASer:CP:POWer:SETPoint?."""
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is True
    assert 'LASer:POWer:SETPoint?' in laser.cmds
    assert 'LASer:CP:POWer:SETPoint?' in laser.cmds


def test_detect_firmware_partial_scpi_treated_as_legacy():
    """If runmode works but no SCPI setpoint probe works, fall back to legacy."""
    laser = FakeLaser(
        firmware='scpi',
        failed_cmds={
            'LASer:CP:POWer:SETPoint?',
            'LASer:PowerModulation:POWer:SETPoint?',
        },
    )
    m = _build_manager(laser)
    m._detect_firmware()
    assert m._scpi is False


def test_detect_firmware_auto_emission_control_is_diagnostic_only(caplog):
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._emission_control = 'auto'
    with caplog.at_level(logging.WARNING, logger='test.Cobolt0601NewLaserManager'):
        m._detect_firmware()

    assert m._scpi is True
    assert m._pause_mode is False
    assert 'las:paus 1' not in laser.cmds
    assert 'diagnostic only' in caplog.text
    info = m.getFirmwareInfo()
    assert info['requestedEmissionControl'] == 'auto'
    assert info['resolvedEmissionControl'] == 'master'


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
    # expressed in mW by default, matching upstream pycobolt.
    assert laser.cmds == [
        '@cobas 0',
        'LASer:PowerModulation:POWer:SETPoint 5.0',
        'LASer:RUNMode PowerModulation',
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

    assert laser.cmds == [
        'LASer:CP:POWer:SETPoint 50.0',
        'LASer:RUNMode ConstantPower',
        'l1',
    ]
    assert m._enabled is True


def test_set_enabled_true_scpi_cp_rejection_falls_back_to_p_not_generic_power():
    laser = FakeLaser(
        firmware='scpi',
        failed_cmds={'LASer:CP:POWer:SETPoint 50.0'},
    )
    m = _build_manager(laser)
    m._scpi = True
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert 'LASer:CP:POWer:SETPoint 50.0' in laser.cmds
    assert 'LASer:POWer:SETPoint 50.0' not in laser.cmds
    assert 'p 0.050000' in laser.cmds
    assert m._enabled is True


def test_cmd_any_caches_winning_variant_for_repeated_power_writes():
    laser = FakeLaser(
        firmware='scpi',
        failed_cmds={'LASer:CP:POWer:SETPoint 50.0'},
    )
    m = _build_manager(laser)
    m._scpi = True

    assert m._set_cw_power_mw(50) is True
    assert m._set_cw_power_mw(75) is True

    assert 'LASer:CP:POWer:SETPoint 50.0' in laser.cmds
    assert 'p 0.050000' in laser.cmds
    assert 'LASer:CP:POWer:SETPoint 75.0' not in laser.cmds
    assert 'p 0.075000' in laser.cmds


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


def test_scan_mode_active_at_zero_setpoint_stays_off():
    """A GUI setpoint of 0 must write modulation power 0 and keep the master
    switch off, NOT fall back to the configured modulationPowerMw default or
    issue l1. Regression test for scan bugs where lasers set to 0 in the
    widget still emitted during scans."""
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser, modulation_power_mw=5.0)
    m._scpi = False
    m._setpoint_mw = 0
    m.setScanModeActive(True)

    assert 'slmp 0.0' in laser.cmds
    assert 'slmp 5.0' not in laser.cmds
    assert 'l0' in laser.cmds
    assert 'l1' not in laser.cmds


def test_set_enabled_true_at_zero_setpoint_flushes_zero_power():
    """Enabling at setpoint 0 must write power 0 to the hardware instead of
    skipping the write and emitting at the last power the laser had."""
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m._setpoint_mw = 0
    m.setEnabled(True)

    assert laser.cmds == ['p 0.000000', 'cp', 'l1']
    assert m._enabled is True


def test_pause_mode_enable_at_zero_setpoint_flushes_zero_power():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    m._setpoint_mw = 0
    m.setEnabled(True)

    assert laser.cmds == [
        'LASer:CP:POWer:SETPoint 0.0',
        'LASer:RUNMode ConstantPower',
        'las:paus 0',
    ]
    assert m._enabled is True


def test_pause_mode_scan_arm_at_zero_setpoint_stays_paused():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True, modulation_power_mw=5.0)
    m._scpi = True
    m._setpoint_mw = 0
    m.setScanModeActive(True)

    assert 'LASer:PowerModulation:POWer:SETPoint 0.0' in laser.cmds
    assert 'LASer:PowerModulation:POWer:SETPoint 5.0' not in laser.cmds
    assert 'las:paus 1' in laser.cmds
    assert 'las:paus 0' not in laser.cmds


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
# Pause mode (emissionControl='pause') — OEM-locked firmware that aborts on l0
# ---------------------------------------------------------------------------


def test_pause_mode_init_gated_and_paused_without_autostart():
    """Pause-mode safe state: enter the modulation safe state (gate on) and
    pause the beam. Must NOT send @cob1 — on the OEM-locked firmware that primes
    a pending turn-on which fires on the next interlock cycle (uncontrolled
    emission). Must also never send l0 or @cobas."""
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, modulation_power_mw=5.0, pause_mode=True)
    m._scpi = True
    m._init_safe_state()

    assert laser.cmds == [
        'LASer:PowerModulation:POWer:SETPoint 5.0',
        'LASer:RUNMode PowerModulation',
        'las:pm:dig:ena 1',
        'las:paus 1',
    ]
    assert '@cob1' not in laser.cmds
    assert 'l0' not in laser.cmds
    assert '@cobas 0' not in laser.cmds
    assert m._enabled is False


def test_pause_mode_enable_resumes_without_l1():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert laser.cmds == [
        'LASer:CP:POWer:SETPoint 50.0',
        'LASer:RUNMode ConstantPower',
        'las:paus 0',
    ]
    assert 'l1' not in laser.cmds
    assert m._enabled is True


def test_pause_mode_disable_pauses_without_l0():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    m._enabled = True
    m.setEnabled(False)

    assert laser.cmds == ['las:paus 1']
    assert 'l0' not in laser.cmds
    assert m._enabled is False


def test_pause_mode_scan_arm_resumes_without_l1():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    m._setpoint_mw = 100
    m.setScanModeActive(True)

    assert 'las:paus 0' in laser.cmds
    assert 'l1' not in laser.cmds
    assert 'l0' not in laser.cmds


def test_pause_mode_finalize_pauses_not_l0():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    m.finalize()

    assert laser.cmds == ['las:paus 1']
    assert 'l0' not in laser.cmds


# ---------------------------------------------------------------------------
# SCPI modulation-power unit pin (mW by default)
# ---------------------------------------------------------------------------


def test_scpi_set_modulation_power_uses_milliwatts_by_default():
    """Matches upstream pycobolt's Cobolt06.set_modulation_power wrapper."""
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._scpi = True
    m.setModulationPower(5)  # 5 mW

    assert 'LASer:PowerModulation:POWer:SETPoint 5.0' in laser.cmds


def test_scpi_set_modulation_power_can_use_watts_override():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._scpi = True
    m._scpi_power_unit = 'w'
    m.setModulationPower(5)  # 5 mW

    assert 'LASer:PowerModulation:POWer:SETPoint 0.005' in laser.cmds


def test_get_modulation_power_parses_cmd_reply():
    laser = FakeLaser(firmware='scpi')
    m = _build_manager(laser)
    m._scpi = True

    assert m.getModulationPower() == 5.0
    assert laser.cmds == ['LASer:PowerModulation:POWer:SETPoint?']


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


# ---------------------------------------------------------------------------
# Phase 1A — reply classifier (the single source of truth for "did it work?")
# ---------------------------------------------------------------------------


def test_classifier_maps_lost_reply_to_command_timeout():
    """PyCoboltManager reports a lost reply as RuntimeError('Syntax Error: No
    response on ...'). Despite the wording that is a TIMEOUT, not a device
    rejection — the command may already have reached the laser."""
    laser = FakeLaser(raise_cmds={
        'l0': RuntimeError('Syntax Error: No response on l0\r')
    })
    with pytest.raises(CommandTimeout):
        send_command(laser, 'l0')


def test_classifier_maps_other_connection_error_to_transport_failure():
    laser = FakeLaser(raise_cmds={'l0': OSError('port disappeared')})
    with pytest.raises(TransportFailure) as excinfo:
        send_command(laser, 'l0')
    assert not isinstance(excinfo.value, CommandTimeout)


def test_classifier_maps_empty_reply_to_unexpected_reply():
    laser = FakeLaser(empty_cmds={'l0'})
    with pytest.raises(UnexpectedReply):
        send_command(laser, 'l0')


def test_classifier_maps_rejection_to_command_rejected():
    laser = FakeLaser(failed_cmds={'l0'})
    with pytest.raises(CommandRejected) as excinfo:
        send_command(laser, 'l0')
    assert 'illegal command' in excinfo.value.reply.lower()


def test_classifier_maps_permission_denied_to_command_rejected():
    """OEM-locked firmware answers '@cobas 0' with a permission error. The
    command did not execute, so this is a rejection rather than a transport
    problem."""
    laser = FakeLaser()
    with pytest.raises(CommandRejected):
        classify_reply('@cobas 0', 'Permission denied')


# ---------------------------------------------------------------------------
# Phase 1A — empty replies are failures, not successes
# ---------------------------------------------------------------------------


def test_empty_reply_fails_closed_on_enable():
    """An empty reply used to count as success, so a half-open port could
    report a laser as enabled without any acknowledgement."""
    laser = FakeLaser(firmware='legacy', empty_cmds={'l1'})
    m = _build_manager(laser)
    m._scpi = False
    m._setpoint_mw = 50
    m.setEnabled(True)

    assert m._enabled is False
    assert 'l0' in laser.cmds      # reverted to the safe state


def test_empty_reply_on_master_off_keeps_enabled_true():
    laser = FakeLaser(firmware='legacy', empty_cmds={'l0'})
    m = _build_manager(laser)
    m._scpi = False
    m._enabled = True
    m.setEnabled(False)

    assert m._enabled is True      # off-transition did not take effect


# ---------------------------------------------------------------------------
# Phase 1A — fallback only after an explicit rejection
# ---------------------------------------------------------------------------


def test_timeout_does_not_trigger_command_fallback():
    """The core safety rule: after a timeout the first command may already
    have reached the device, so the fallback variant must NOT be sent."""
    laser = FakeLaser(
        firmware='scpi',
        raise_cmds={
            'LASer:CP:POWer:SETPoint 50.0':
                RuntimeError('Syntax Error: No response on ...')
        },
    )
    m = _build_manager(laser)
    m._scpi = True

    assert m._set_cw_power_mw(50) is False
    assert 'LASer:CP:POWer:SETPoint 50.0' in laser.cmds
    assert 'p 0.050000' not in laser.cmds      # fallback must not be tried


def test_transport_failure_does_not_trigger_command_fallback():
    laser = FakeLaser(
        firmware='scpi',
        raise_cmds={'LASer:CP:POWer:SETPoint 50.0': OSError('port gone')},
    )
    m = _build_manager(laser)
    m._scpi = True

    assert m._set_cw_power_mw(50) is False
    assert 'p 0.050000' not in laser.cmds


def test_empty_reply_does_not_trigger_command_fallback():
    laser = FakeLaser(
        firmware='scpi', empty_cmds={'LASer:CP:POWer:SETPoint 50.0'},
    )
    m = _build_manager(laser)
    m._scpi = True

    assert m._set_cw_power_mw(50) is False
    assert 'p 0.050000' not in laser.cmds


def test_explicit_rejection_still_triggers_command_fallback():
    """The contrast case: a rejected command provably did not execute, so
    trying the documented alternative is safe and must still happen."""
    laser = FakeLaser(
        firmware='scpi', failed_cmds={'LASer:CP:POWer:SETPoint 50.0'},
    )
    m = _build_manager(laser)
    m._scpi = True

    assert m._set_cw_power_mw(50) is True
    assert 'p 0.050000' in laser.cmds


def test_timeout_on_cached_variant_does_not_try_other_variants():
    """Once a variant is cached, a later timeout on it must not silently
    re-open the search and send a different mutating command."""
    laser = FakeLaser(
        firmware='scpi', failed_cmds={'LASer:CP:POWer:SETPoint 50.0'},
    )
    m = _build_manager(laser)
    m._scpi = True
    assert m._set_cw_power_mw(50) is True       # caches the 'p ...' variant

    laser.cmds.clear()
    laser.raise_cmds = {'p 0.075000': RuntimeError('timed out')}
    assert m._set_cw_power_mw(75) is False
    assert laser.cmds == ['p 0.075000']         # no other variant attempted


# ---------------------------------------------------------------------------
# Phase 1A — unidentified device is not guessed at
# ---------------------------------------------------------------------------


def test_detect_firmware_all_probes_fail_raises():
    """Previously defaulted to the legacy command set. Guessing a dialect for
    an unidentified laser is exactly the silent assumption to avoid."""
    laser = FakeLaser(
        firmware='legacy',
        failed_cmds={'LASer:RUNMode?', 'LASer:POWer:SETPoint?',
                     'LASer:CP:POWer:SETPoint?',
                     'LASer:PowerModulation:POWer:SETPoint?', 'l?', 'gam?'},
    )
    m = _build_manager(laser)
    with pytest.raises(DeviceInitializationError) as excinfo:
        m._detect_firmware()
    assert 'simulation' in str(excinfo.value)


def test_init_safe_state_raises_when_master_off_fails():
    laser = FakeLaser(firmware='legacy', failed_cmds={'l0'})
    m = _build_manager(laser)
    m._scpi = False
    with pytest.raises(DeviceInitializationError):
        m._init_safe_state()


def test_init_safe_state_raises_when_pause_fails():
    laser = FakeLaser(firmware='scpi', failed_cmds={'las:paus 1'})
    m = _build_manager(laser, pause_mode=True)
    m._scpi = True
    with pytest.raises(DeviceInitializationError):
        m._init_safe_state()


# ---------------------------------------------------------------------------
# Phase 1A — finalization releases the port
# ---------------------------------------------------------------------------


def test_finalize_closes_the_connection():
    laser = FakeLaser(firmware='legacy')
    m = _build_manager(laser)
    m._scpi = False
    m.finalize()

    assert laser.cmds == ['l0']
    assert laser.disconnected is True


def test_finalize_closes_the_connection_even_if_safe_off_fails():
    """A laser that will not go dark must still release the port, or the next
    session cannot reconnect to diagnose it."""
    laser = FakeLaser(
        firmware='legacy', raise_cmds={'l0': OSError('port gone')},
    )
    m = _build_manager(laser)
    m._scpi = False
    m.finalize()

    assert laser.disconnected is True


# ---------------------------------------------------------------------------
# Phase 1A — simulation is explicit
# ---------------------------------------------------------------------------


class FakeLaserInfo:
    """Minimal stand-in for the LaserInfo dataclass."""

    def __init__(self, **managerProperties):
        self.managerProperties = managerProperties
        self.wavelength = 488
        self.valueRangeMin = 0
        self.valueRangeMax = 100
        self.valueRangeStep = 1


def test_simulation_true_uses_mock_without_opening_the_port():
    info = FakeLaserInfo(digitalPorts=['COM_DOES_NOT_EXIST'], simulation=True)
    m = Cobolt0601NewLaserManager(info, 'simulated')

    assert m._simulation is True
    assert m._real_hw is False
    assert m._laser.cmds[-1] == 'l0'        # reached the safe state
    assert m._enabled is False


def test_absent_simulation_key_defaults_to_false_and_raises():
    """The migration-critical case: no existing setup file has a simulation
    key, and a missing device must fail loudly instead of loading a mock that
    looks like a working laser."""
    info = FakeLaserInfo(digitalPorts=['COM_DOES_NOT_EXIST'])
    with pytest.raises(DeviceInitializationError) as excinfo:
        Cobolt0601NewLaserManager(info, 'real')

    message = str(excinfo.value)
    assert 'COM_DOES_NOT_EXIST' in message
    assert 'simulation' in message          # tells the operator the way out


def test_simulation_false_raises_rather_than_loading_mock():
    info = FakeLaserInfo(digitalPorts=['COM_DOES_NOT_EXIST'], simulation=False)
    with pytest.raises(DeviceInitializationError):
        Cobolt0601NewLaserManager(info, 'real')


def test_shipped_example_setup_declares_simulation():
    """example_kiralux_teensy.json is built by the example-setup UI test on
    machines that cannot have COM17/COM4, so it must declare simulation
    rather than rely on a silent mock fallback."""
    import json
    import os
    from imswitch.imcommon.model.dirtools import DataFileDirs

    path = os.path.join(DataFileDirs.UserDefaults, 'imcontrol_setups',
                        'example_kiralux_teensy.json')
    with open(path) as handle:
        setup = json.load(handle)

    cobolts = [laser for laser in setup['lasers'].values()
               if laser['managerName'] == 'Cobolt0601NewLaserManager']
    assert cobolts
    for laser in cobolts:
        assert laser['managerProperties'].get('simulation') is True


def test_mock_cobolt06_send_cmd_scpi_mode_accepts_scpi():
    from imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601 import (
        MockCobolt06,
    )
    mock = MockCobolt06('COM_TEST')
    mock.firmware = 'scpi'
    assert mock.send_cmd('LASer:RUNMode?') == 'ConstantPower'

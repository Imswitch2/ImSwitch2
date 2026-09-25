"""Tests for AAAOTFLaserManager — command extraction and TTL behavior.

The AA refactor is a command-extraction cleanup, not a discovery system: the
controller has no safe read-only dialect query, so there is one explicit
compatibility profile and nothing is guessed.

These tests pin the wire traces the manager has always produced, so the
extraction can be shown to be faithful. Reply handling stays deliberately
permissive — a completed exchange is success regardless of returned text —
because the real acknowledgement contract is not yet known from hardware.
Strict acknowledgement, rejection and malformed-reply cases arrive with the
later evidence-backed change.
"""

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.lasers.AAAOTFLaserManager import (
    AAAOTFLaserManager,
)
from imswitch.imcontrol.model.managers.lasers.aa_aotf_protocols import (
    validate_amplitude,
    validate_channel,
)
from imswitch.imcontrol.model.managers.lasers.aa_aotf_protocols.frequency_startup import (
    format_frequency_mhz,
    validate_frequency_mhz,
)
from imswitch.imcontrol.model.managers.lasers._protocol import (
    CommandTimeout,
    DeviceInitializationError,
    TransportFailure,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRS232:
    """Records every query. Shared by all channels, like the real manager."""

    def __init__(self, reply: str = 'OK', raise_cmds: dict = None):
        self.cmds: list = []
        self.reply = reply
        self.raise_cmds: dict = dict(raise_cmds or {})

    def query(self, command: str) -> str:
        self.cmds.append(command)
        if command in self.raise_cmds:
            raise self.raise_cmds[command]
        return self.reply

    def write(self, command: str):
        self.cmds.append(command)
        if command in self.raise_cmds:
            raise self.raise_cmds[command]


class FakeLaserInfo:
    def __init__(self, **managerProperties):
        self.managerProperties = managerProperties
        self.wavelength = 561
        self.valueRangeMin = 0
        self.valueRangeMax = 1023
        self.valueRangeStep = 1


def _build(rs232=None, name='561AOTF', **properties):
    """Build a manager against a fake RS232 connection."""
    rs232 = rs232 if rs232 is not None else FakeRS232()
    properties.setdefault('channel', 1)
    properties.setdefault('rs232device', 'aaaotf')
    info = FakeLaserInfo(**properties)
    manager = AAAOTFLaserManager(
        info, name, rs232sManager={'aaaotf': rs232}
    )
    return manager, rs232


# ---------------------------------------------------------------------------
# Profile selection — no discovery, explicit default
# ---------------------------------------------------------------------------


def test_omitted_protocol_profile_selects_compatibility_profile():
    """Omission is the upgrade path for every existing AA setup file."""
    m, _ = _build()
    assert m._profile.profile_id == 'aa.compatibility'


def test_explicit_profile_can_be_requested():
    m, _ = _build(protocolProfile='aa.compatibility')
    assert m._profile.profile_id == 'aa.compatibility'


def test_frequency_startup_profile_can_be_requested():
    m, _ = _build(protocolProfile='aa.frequency-startup')
    assert m._profile.profile_id == 'aa.frequency-startup'


def test_unknown_profile_is_rejected():
    with pytest.raises(DeviceInitializationError) as excinfo:
        _build(protocolProfile='aa.nonexistent')
    assert 'aa.compatibility' in str(excinfo.value)


def test_compatibility_profile_is_not_auto_selectable():
    """There is no safe read-only discriminator, so nothing may probe for it."""
    m, _ = _build()
    assert m._profile.auto_selectable is False


# ---------------------------------------------------------------------------
# Construction — initial control mode
# ---------------------------------------------------------------------------


def test_init_defaults_to_internal_control():
    _, rs232 = _build()
    assert rs232.cmds == ['L1I1O0']


def test_init_with_toggle_true_external_selects_external():
    _, rs232 = _build(toggleTrueExternal=True)
    assert rs232.cmds == ['L1I0']


def test_init_with_ttl_toggling_selects_external():
    _, rs232 = _build(ttlToggling=True)
    assert rs232.cmds == ['L1I0']


def test_init_with_both_flags_selects_internal():
    _, rs232 = _build(toggleTrueExternal=True, ttlToggling=True)
    assert rs232.cmds == ['L1I1O0']


def test_frequency_startup_is_opt_in_and_restores_frequency_before_control_mode():
    m, rs232 = _build(
        protocolProfile='aa.frequency-startup', frequencyMHz=143.0
    )
    assert m._frequency_mhz == 143.0
    assert rs232.cmds == ['I0', 'L1F143.0', 'L1I1O0']


def test_frequency_startup_uses_configured_channel_and_fractional_frequency():
    _, rs232 = _build(
        channel=3,
        protocolProfile='aa.frequency-startup',
        frequencyMHz='143.25',
    )
    assert rs232.cmds == ['I0', 'L3F143.25', 'L3I1O0']


def test_frequency_profile_without_frequency_preserves_legacy_trace():
    _, rs232 = _build(protocolProfile='aa.frequency-startup')
    assert rs232.cmds == ['L1I1O0']


def test_zero_frequency_editor_default_preserves_legacy_trace():
    _, rs232 = _build(frequencyMHz=0.0)
    assert rs232.cmds == ['L1I1O0']


# ---------------------------------------------------------------------------
# Channel enable / disable
# ---------------------------------------------------------------------------


def test_set_enabled_true_command_sequence():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setEnabled(True)
    assert rs232.cmds == ['L1O1']


def test_set_enabled_false_command_sequence():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setEnabled(False)
    assert rs232.cmds == ['L1O0']


def test_set_enabled_returns_true_on_success():
    m, _ = _build()
    assert m.setEnabled(True) is True
    assert m.setEnabled(False) is True


def test_set_enabled_returns_false_on_rejection_without_raising():
    """A caller such as the scan arming path relies on this bool to warn
    instead of assuming a rejected enable succeeded. ``_run`` returns False
    (rather than raising) when the profile has no such operation --
    triggered here with a stand-in profile, since every real profile
    implements ``set_channel_enabled``."""
    m, _ = _build()

    class _ProfileWithoutEnable:
        profile_id = 'test.no-enable'

    m._profile = _ProfileWithoutEnable()
    assert m.setEnabled(True) is False


def test_channel_index_is_used_in_every_command():
    m, rs232 = _build(channel=3)
    rs232.cmds.clear()
    m.setEnabled(True)
    m.setValue(500)
    m.externalControl()
    m.internalControl()
    assert rs232.cmds == ['L3O1', 'L3P500', 'L3I0', 'L3I1O0']


# ---------------------------------------------------------------------------
# Power / amplitude
# ---------------------------------------------------------------------------


def test_set_value_without_lut_rounds_to_nearest_amplitude():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setValue(511.6)
    assert rs232.cmds == ['L1P512']


def test_set_value_negative_is_refused_rather_than_sent():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setValue(-5)
    assert rs232.cmds == []


def test_set_value_non_numeric_is_refused_rather_than_sent():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setValue('not-a-number')
    assert rs232.cmds == []


# ---------------------------------------------------------------------------
# Internal / external control and scan mode
# ---------------------------------------------------------------------------


def test_scan_mode_active_switches_to_external_control():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setScanModeActive(True)
    assert rs232.cmds == ['L1I0']


def test_scan_mode_inactive_returns_to_internal_control_when_that_is_idle():
    """Both flags default false -> idle mode is internal."""
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setScanModeActive(False)
    assert rs232.cmds == ['L1I1O0']


def test_scan_mode_inactive_restores_external_idle_mode():
    """ttlToggling alone idles external (the TTL-gating configuration). A
    scan end must not silently re-park it internal -- that disabled TTL
    gating until the next manual write happened to select external again."""
    m, rs232 = _build(ttlToggling=True)
    rs232.cmds.clear()
    m.setScanModeActive(True)
    rs232.cmds.clear()
    m.setScanModeActive(False)
    assert rs232.cmds == ['L1I0']


def test_scan_mode_inactive_restores_internal_idle_mode_when_both_flags_set():
    """Both flags true -> idle mode is internal (see the init tests)."""
    m, rs232 = _build(toggleTrueExternal=True, ttlToggling=True)
    rs232.cmds.clear()
    m.setScanModeActive(True)
    rs232.cmds.clear()
    m.setScanModeActive(False)
    assert rs232.cmds == ['L1I1O0']


# ---------------------------------------------------------------------------
# TTL toggling — control mode is flipped around each write
# ---------------------------------------------------------------------------


def test_ttl_toggling_wraps_enable_with_control_mode_changes():
    m, rs232 = _build(ttlToggling=True)
    rs232.cmds.clear()
    m.setEnabled(True)
    # Internal to issue the command, external again so TTL can drive it.
    assert rs232.cmds == ['L1I1O0', 'L1O1', 'L1I0']


def test_ttl_toggling_wraps_set_value_with_control_mode_changes():
    m, rs232 = _build(ttlToggling=True)
    rs232.cmds.clear()
    m.setValue(500)
    assert rs232.cmds == ['L1I1O0', 'L1P500', 'L1I0']


def test_ttl_toggling_with_toggle_true_external_inverts_the_wrap():
    m, rs232 = _build(ttlToggling=True, toggleTrueExternal=True)
    rs232.cmds.clear()
    m.setValue(500)
    assert rs232.cmds == ['L1I0', 'L1P500', 'L1I1O0']


def test_without_ttl_toggling_no_control_mode_commands_are_added():
    m, rs232 = _build()
    rs232.cmds.clear()
    m.setValue(500)
    assert rs232.cmds == ['L1P500']


def test_ttl_toggling_power_write_restores_emission_when_channel_is_on():
    """select_internal_control's LnI1O0 turns emission off as a side effect
    of issuing the write. If the channel was confirmed on, that must not
    leave it dark under the external park while the widget still reads ON."""
    m, rs232 = _build(ttlToggling=True)
    m.setEnabled(True)
    rs232.cmds.clear()

    m.setValue(500)

    assert rs232.cmds == ['L1I1O0', 'L1P500', 'L1O1', 'L1I0']


def test_ttl_toggling_power_write_does_not_resend_enable_when_channel_is_off():
    m, rs232 = _build(ttlToggling=True)
    rs232.cmds.clear()

    m.setValue(500)

    assert rs232.cmds == ['L1I1O0', 'L1P500', 'L1I0']


def test_ttl_toggling_with_toggle_true_external_power_write_does_not_resend_enable():
    """This direction issues the write in *external* control, which does not
    clobber the O state -- no restore is needed even if the channel is on."""
    m, rs232 = _build(ttlToggling=True, toggleTrueExternal=True)
    m.setEnabled(True)
    rs232.cmds.clear()

    m.setValue(500)

    assert rs232.cmds == ['L1I0', 'L1P500', 'L1I1O0']


def test_ttl_toggling_power_write_after_disable_does_not_resend_enable():
    m, rs232 = _build(ttlToggling=True)
    m.setEnabled(True)
    m.setEnabled(False)
    rs232.cmds.clear()

    m.setValue(500)

    assert rs232.cmds == ['L1I1O0', 'L1P500', 'L1I0']


# ---------------------------------------------------------------------------
# Calibration lookup
# ---------------------------------------------------------------------------


@pytest.fixture
def calib_csv(tmp_path):
    """Raw amplitude in column 0, measured power in column 1."""
    path = tmp_path / 'calib.csv'
    np.savetxt(path, np.array([
        [0.0, 0.0],
        [500.0, 5.0],
        [1000.0, 10.0],
    ]))
    return str(path)


def test_calibration_converts_percentage_to_amplitude(calib_csv):
    m, rs232 = _build(calibCsvPath=calib_csv)
    assert m.valueUnits == '%'
    rs232.cmds.clear()
    m.setValue(50)          # 50 % of the calibrated span
    assert rs232.cmds == ['L1P500']


def test_calibration_rejects_above_range_without_sending(calib_csv):
    """An excessive request must not be converted into maximum output."""
    m, rs232 = _build(calibCsvPath=calib_csv)
    rs232.cmds.clear()
    m.setValue(150)
    assert rs232.cmds == []


def test_calibration_rejects_below_range_without_sending(calib_csv):
    m, rs232 = _build(calibCsvPath=calib_csv)
    rs232.cmds.clear()
    m.setValue(-10)
    assert rs232.cmds == []


def test_missing_calibration_leaves_arbitrary_units():
    m, _ = _build()
    assert m.valueUnits == 'arb'


# ---------------------------------------------------------------------------
# Invalid configuration
# ---------------------------------------------------------------------------


def test_channel_zero_is_rejected_at_construction():
    with pytest.raises(DeviceInitializationError) as excinfo:
        _build(channel=0)
    assert 'starts at 1' in str(excinfo.value)


def test_non_numeric_channel_is_rejected_at_construction():
    with pytest.raises(DeviceInitializationError):
        _build(channel='left')


def test_frequency_requires_frequency_startup_profile():
    with pytest.raises(DeviceInitializationError) as excinfo:
        _build(frequencyMHz=143.0)
    assert 'aa.frequency-startup' in str(excinfo.value)


@pytest.mark.parametrize('frequency', [-1, 'not-a-number', float('nan')])
def test_invalid_startup_frequency_is_rejected(frequency):
    with pytest.raises(DeviceInitializationError):
        _build(
            protocolProfile='aa.frequency-startup',
            frequencyMHz=frequency,
        )


def test_validate_channel_and_amplitude_helpers():
    assert validate_channel('2') == 2
    assert validate_amplitude('512') == 512
    with pytest.raises(ValueError):
        validate_channel(0)
    with pytest.raises(ValueError):
        validate_amplitude(-1)


def test_frequency_validation_and_wire_format():
    assert validate_frequency_mhz('143.25') == 143.25
    assert format_frequency_mhz(143) == '143.0'
    assert format_frequency_mhz(143.25) == '143.25'
    with pytest.raises(ValueError):
        validate_frequency_mhz(float('inf'))


# ---------------------------------------------------------------------------
# Reply handling — permissive by design, transport failures still surface
# ---------------------------------------------------------------------------


def test_any_completed_reply_counts_as_success():
    """Deliberately permissive: the manager has always discarded reply text,
    so a controller that works today must keep working. Tightening this needs
    a hardware transcript, not a guess."""
    m, rs232 = _build(rs232=FakeRS232(reply='whatever the device says'))
    rs232.cmds.clear()
    assert m._run('set_channel_enabled', True) is True
    assert rs232.cmds == ['L1O1']


def test_empty_reply_is_not_treated_as_failure():
    m, rs232 = _build(rs232=FakeRS232(reply=''))
    rs232.cmds.clear()
    assert m._run('set_channel_enabled', True) is True


def test_transport_failure_propagates_from_manager():
    rs232 = FakeRS232(raise_cmds={'L1O1': OSError('port gone')})
    m, _ = _build(rs232=rs232)
    rs232.cmds.clear()
    with pytest.raises(TransportFailure):
        m.setEnabled(True)
    assert rs232.cmds == ['L1O1']


def test_transport_exceptions_map_to_the_shared_taxonomy():
    from imswitch.imcontrol.model.managers.lasers.aa_aotf_protocols import (
        send_query,
    )

    with pytest.raises(CommandTimeout):
        send_query(FakeRS232(raise_cmds={'L1O1': TimeoutError('timed out')}),
                   'L1O1')
    with pytest.raises(TransportFailure):
        send_query(FakeRS232(raise_cmds={'L1O1': OSError('port gone')}),
                   'L1O1')


def test_strict_frequency_startup_write_failure_is_a_startup_error():
    rs232 = FakeRS232(raise_cmds={'I0': OSError('port gone')})
    with pytest.raises(DeviceInitializationError) as excinfo:
        _build(
            rs232=rs232,
            protocolProfile='aa.frequency-startup',
            frequencyMHz=143.0,
            useMockOnFailure=False,
        )
    assert isinstance(excinfo.value.__cause__, TransportFailure)
    assert rs232.cmds == ['I0', 'L1O0']


# ---------------------------------------------------------------------------
# Startup without an answering controller — mock fallback
# ---------------------------------------------------------------------------


def _timeout(command):
    """What pyvisa raises when the controller never answers."""
    return {command: TimeoutError('VI_ERROR_TMO (-1073807339): Timeout expired')}


def test_unanswered_startup_continues_in_mock_mode_after_an_off(caplog):
    """A pyvisa timeout at startup used to propagate out of the manager and
    abort ImSwitch."""
    rs232 = FakeRS232(raise_cmds=_timeout('L1I1O0'))
    with caplog.at_level('WARNING'):
        m, _ = _build(rs232=rs232)
    assert m._isMock is True
    assert rs232.cmds == ['L1I1O0', 'L1O0']
    assert 'mock mode' in caplog.text
    assert not [r for r in caplog.records if r.levelname == 'CRITICAL']


def test_mock_mode_sends_nothing_further():
    rs232 = FakeRS232(raise_cmds=_timeout('L1I0'))
    m, _ = _build(rs232=rs232, ttlToggling=True)
    rs232.cmds.clear()
    m.setValue(500)
    m.setEnabled(True)
    m.setScanModeActive(True)
    m.setScanModeActive(False)
    m.setEnabled(False)
    assert rs232.cmds == []


def test_unacknowledged_off_is_reported_as_critical(caplog):
    rs232 = FakeRS232(raise_cmds={**_timeout('L1I1O0'), **_timeout('L1O0')})
    with caplog.at_level('WARNING'):
        m, _ = _build(rs232=rs232)
    assert m._isMock is True
    assert rs232.cmds == ['L1I1O0', 'L1O0']
    critical = [r for r in caplog.records if r.levelname == 'CRITICAL']
    assert critical and 'was not acknowledged' in critical[0].getMessage()


@pytest.mark.parametrize('strict', [False, 'false', 'no', 0])
def test_strict_startup_raises_a_startup_error_after_the_off(strict):
    rs232 = FakeRS232(raise_cmds=_timeout('L1I1O0'))
    with pytest.raises(DeviceInitializationError, match='useMockOnFailure') as excinfo:
        _build(rs232=rs232, useMockOnFailure=strict)
    assert isinstance(excinfo.value.__cause__, CommandTimeout)
    assert rs232.cmds == ['L1I1O0', 'L1O0']


def test_frequency_startup_failure_falls_back_to_mock_by_default():
    rs232 = FakeRS232(raise_cmds={'I0': OSError('port gone')})
    m, _ = _build(
        rs232=rs232,
        protocolProfile='aa.frequency-startup',
        frequencyMHz=143.0,
    )
    assert m._isMock is True
    assert rs232.cmds == ['I0', 'L1O0']


def test_a_configuration_error_still_aborts_even_with_mock_fallback():
    """Mock mode is for a controller that does not answer, not a bad file."""
    with pytest.raises(DeviceInitializationError):
        _build(protocolProfile='aa.nonexistent', useMockOnFailure=True)


def test_a_failure_after_startup_still_propagates():
    """Only the startup exchange falls back; a live channel that stops
    answering must not look successful to the GUI."""
    rs232 = FakeRS232(raise_cmds={'L1O1': OSError('port gone')})
    m, _ = _build(rs232=rs232)
    assert m._isMock is False
    with pytest.raises(TransportFailure):
        m.setEnabled(True)


# ---------------------------------------------------------------------------
# Structural guard
# ---------------------------------------------------------------------------


def test_manager_builds_no_vendor_command_strings():
    """The AA manager must not construct L<channel>... commands any more."""
    import ast
    import inspect

    from imswitch.imcontrol.model.managers.lasers import (
        AAAOTFLaserManager as module,
    )

    tree = ast.parse(inspect.getsource(module))

    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstring_ids.add(id(first.value))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids):
            assert not node.value.startswith('L'), (
                f'{node.value!r} looks like an AA command and belongs in a '
                f'profile file'
            )
        # f-strings assembling a command would appear as JoinedStr
        if isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values
                     if isinstance(v, ast.Constant)]
            assert not any(p.startswith('L') for p in parts), (
                'an AA command is being assembled in the manager'
            )


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Scan laser participation, gate/power pairing and teardown (rig findings).

The STED setup splits one physical laser across two ImSwitch entries: a bare
NI-DAQ digital-line placeholder that the scan gates (``561``), and an AOTF
channel that holds the power over RS232 (``561AOTF``). Only the gate carries a
TTL line, so only the gate ever appears in the scan's device list — and nothing
switched the AOTF on, so the scan gated a beam nobody had turned on. The laser
emitted only when the user enabled the AOTF by hand.

The gate now declares its partner via ``LaserInfo.powerDevice`` and the scan
arms both. Other defects fixed alongside, all previously silent:

* participation was read from the NI-DAQ AO/DO device list, which an AOTF with
  no channels of its own can never appear in;
* teardown ran ``setScanModeActive(False)`` on EVERY laser, which zeroes the
  output on managers with an analog channel and so destroyed the setpoint of an
  AOM the scan had never touched;
* arming wrote the power from the widget, overwriting a good amplitude with a
  bad read.

Contract: a laser in the scan's TTL list is gated by the scan and its paired
power device is switched on, both regardless of their Live toggles and both
off afterwards. The whole LaserWidget is read-only while a scan is active.
Lasers outside the scan-owned gate/power paths are never touched.

Between the parts of a multi-part run (timelapse timepoints), the run stays
armed but the power devices the arming switched on are paused on each part's
``sigScanDone`` and switched back on when the next part republishes
membership — so the sample sits in darkness through the waits. Repeat frames
never publish a part terminal and are unaffected.
"""



import inspect

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers._triggerscope_scan_lifecycle import (
    TriggerScopeScanLifecycleMixin,
)
from imswitch.imcontrol.controller.controllers.LaserController import (
    LaserController,
)
from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.model import (
    LaserPowerContext, LaserPowerMode, LaserScanPowerControl, LaserScanState,
)


class _Laser:
    """``zeroesOnExit`` models NidaqLaserManager, whose setScanModeActive(False)
    is implemented as setValue(0) and so discards the user's setpoint."""

    def __init__(self, name, zeroesOnExit=False):
        self.name = name
        self.calls = []
        self._zeroesOnExit = zeroesOnExit
        self.isBinary = False

    def setScanModeActive(self, active):
        self.calls.append(('scanMode', active))
        if not active and self._zeroesOnExit:
            self.calls.append(('value', 0))

    def setEnabled(self, enabled):
        self.calls.append(('enabled', enabled))

    def setValue(self, value):
        # AAAOTFLaserManager and most other managers define exactly this
        # signature; passing enabled/for_scanning keywords raises TypeError.
        self.calls.append(('value', value))


class _LasersManager:
    def __init__(self, names, zeroesOnExit=()):
        self._lasers = {name: _Laser(name, name in zeroesOnExit)
                        for name in names}

    def __iter__(self):
        return iter(self._lasers.items())

    def __getitem__(self, name):
        return self._lasers[name]


class _Master:
    def __init__(self, lasersManager):
        self.lasersManager = lasersManager


class _Widget:
    def __init__(self):
        self.enableEditable = {}
        self.active = {}
        self.values = {}
        self.editable = True
        self.resumeAvailable = False

    def setEditable(self, editable):
        self.editable = bool(editable)

    def setResumeLiveAvailable(self, available):
        self.resumeAvailable = bool(available)

    def setLaserEnableEditable(self, name, editable):
        self.enableEditable[name] = editable

    def setLaserActive(self, name, active, emitSignal=True):
        self.active[name] = active

    def getValue(self, name):
        return self.values.get(name, 10.0)

    def setValue(self, name, value, emitSignal=True):
        self.values[name] = value

    def isLaserActive(self, name):
        return bool(self.active.get(name, False))


class _LaserInfo:
    def __init__(self, powerDevice=None):
        self.powerDevice = powerDevice


class _SetupInfo:
    def __init__(self, lasers, laserPowerMode='separated'):
        self.lasers = lasers
        self.deviceCategorySettings = {
            'lasers': {'powerMode': laserPowerMode}
        }


class _SharedAttrs(dict):
    pass


class _Workflow:
    def __init__(self):
        self.error = None

    def set_laser_scan_preparation_error(self, message):
        self.error = message

    def clear_laser_scan_preparation_error(self):
        self.error = None

    def laser_scan_preparation_error(self):
        return self.error


class _CommChannel:
    def __init__(self):
        self.sharedAttrs = _SharedAttrs()
        self.scanWorkflow = _Workflow()


class _Logger:
    def __init__(self):
        self.messages = []

    def debug(self, message, *args, **kwargs):
        self.messages.append(('debug', message))

    def info(self, message, *args, **kwargs):
        self.messages.append(('info', message))

    def warning(self, message, *args, **kwargs):
        self.messages.append(('warning', message))

    def error(self, message, *args, **kwargs):
        self.messages.append(('error', message))


def _controller(names=('561', '561AOTF'), pairs=None, zeroesOnExit=(), mode='separated'):
    """``pairs`` maps a gate laser to the device that sets its power."""
    pairs = pairs or {}
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = _Master(_LasersManager(names, zeroesOnExit))
    ctrl._widget = _Widget()
    ctrl._logger = _Logger()
    ctrl._commChannel = _CommChannel()
    ctrl.settingAttr = False
    ctrl.is_scanning = False
    ctrl._livePowerValues = {}
    ctrl._scanPowerValues = {}
    ctrl._scanLaserState = {}
    ctrl._scanLiveEnabledSnapshot = None
    ctrl._scanLiveSuppressed = False
    ctrl._resumableLiveEnabledSnapshot = None
    ctrl._scanPreparationFailed = None
    ctrl._scanPowerWrittenLasers = []
    ctrl._scanArmedLasers = []
    ctrl._scanEnabledLasers = []
    ctrl._scanLasersPaused = False
    ctrl._powerMode = LaserPowerMode(mode)
    ctrl._currentPowerContext = LaserPowerContext.LIVE
    ctrl._selectedPresetByContext = {}
    ctrl._selectedCoupledPreset = None
    ctrl._setupInfo = _SetupInfo(
        {name: _LaserInfo(pairs.get(name)) for name in names},
        laserPowerMode=mode,
    )
    return ctrl


# --------------------------------------------------------------------------- #
# Coupled parity with main@bf980335                                             #
# --------------------------------------------------------------------------- #

def test_coupled_arming_never_writes_power_and_syncs_power_device_on():
    ctrl = _controller(
        names=('561', '561AOTF'), pairs={'561': '561AOTF'}, mode='coupled'
    )
    ctrl._widget.values['561AOTF'] = 17.0
    ctrl._scanPowerValues['561AOTF'] = 99.0  # dormant separated state

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ('value', 17.0) not in ctrl._master.lasersManager['561AOTF'].calls
    assert ('value', 99.0) not in ctrl._master.lasersManager['561AOTF'].calls
    assert not any(call[0] == 'value' for call in ctrl._master.lasersManager['561'].calls)
    assert ('scanMode', True) in ctrl._master.lasersManager['561'].calls
    assert ('enabled', True) in ctrl._master.lasersManager['561AOTF'].calls
    assert ctrl._widget.active['561AOTF'] is True
    assert ctrl._widget.enableEditable['561'] is False
    assert ctrl._widget.enableEditable['561AOTF'] is False


def test_coupled_scan_start_does_not_globally_lock_or_suppress_live():
    ctrl = _controller(names=('488', '640'), mode='coupled')
    ctrl._widget.active.update({'488': True, '640': True})

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._widget.editable is True
    assert ('enabled', False) not in ctrl._master.lasersManager['488'].calls
    assert ctrl._master.lasersManager['640'].calls == []
    assert ctrl._widget.active['640'] is True


def test_coupled_run_clears_stale_preparation_veto_and_never_recreates_it():
    """Unconditional TriggerScope veto checks remain a no-op in coupled mode."""
    ctrl = _controller(names=('488',), mode='coupled')
    workflow = ctrl._commChannel.scanWorkflow
    workflow.error = 'stale separated preparation failure'

    LaserController.scanChanged(ctrl, True)
    assert workflow.laser_scan_preparation_error() is None

    LaserController.scanDevicesResolved(ctrl, ['488'])
    assert workflow.laser_scan_preparation_error() is None

    LaserController.scanPartDone(ctrl)
    assert workflow.laser_scan_preparation_error() is None


def test_coupled_nonparticipant_remains_editable_and_untouched_on_teardown():
    ctrl = _controller(names=('488', '640'), mode='coupled')
    ctrl._widget.active['640'] = True
    ctrl._widget.values['488'] = 12.0

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['640'].calls == []
    assert ctrl._widget.active['640'] is True
    assert ctrl._widget.enableEditable.get('640', True) is True
    assert ('scanMode', False) in ctrl._master.lasersManager['488'].calls
    assert ('enabled', False) in ctrl._master.lasersManager['488'].calls
    assert ('value', 12.0) in ctrl._master.lasersManager['488'].calls


def test_coupled_repeat_membership_does_not_rearm_or_write_power():
    ctrl = _controller(
        names=('561', '561AOTF'), pairs={'561': '561AOTF'}, mode='coupled'
    )
    LaserController.scanDevicesResolved(ctrl, ['561'])
    firstGateCalls = list(ctrl._master.lasersManager['561'].calls)
    firstPowerCalls = list(ctrl._master.lasersManager['561AOTF'].calls)

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['561'].calls == firstGateCalls
    assert ctrl._master.lasersManager['561AOTF'].calls == firstPowerCalls


def test_coupled_multipart_pause_resume_syncs_power_device_widget():
    ctrl = _controller(
        names=('561', '561AOTF'), pairs={'561': '561AOTF'}, mode='coupled'
    )
    LaserController.scanDevicesResolved(ctrl, ['561'])
    LaserController.scanPartDone(ctrl)
    assert ctrl._widget.active['561AOTF'] is False

    LaserController.scanDevicesResolved(ctrl, ['561'])
    assert ctrl._widget.active['561AOTF'] is True
    assert ctrl._master.lasersManager['561'].calls.count(('scanMode', True)) == 1


def test_coupled_teardown_preserves_prework_zero_setpoint_rule():
    ctrl = _controller(names=('488',), mode='coupled')
    ctrl._widget.values['488'] = 0.0

    LaserController.scanDevicesResolved(ctrl, ['488'])
    LaserController.scanChanged(ctrl, False)

    # main@bf980335 deliberately did not write a falsey/zero widget setpoint
    # back during teardown. Coupled preserves that exact historical behavior.
    assert ('value', 0.0) not in ctrl._master.lasersManager['488'].calls
    assert ('scanMode', False) in ctrl._master.lasersManager['488'].calls
    assert ('enabled', False) in ctrl._master.lasersManager['488'].calls


def test_coupled_never_creates_resume_snapshot():
    ctrl = _controller(names=('488',), mode='coupled')
    ctrl._widget.active['488'] = True

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    LaserController.scanChanged(ctrl, False)

    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False


# --------------------------------------------------------------------------- #
# Participation comes from the TTL list                                        #
# --------------------------------------------------------------------------- #

def test_gate_and_its_power_device_are_both_armed():
    """The whole point: only the gate carries a TTL line and so only the gate
    appears in the scan list, but the AOTF is what actually emits."""
    ctrl = _controller(names=('561', '561AOTF'),
                       pairs={'561': '561AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561'].calls
    assert ('enabled', True) in ctrl._master.lasersManager['561AOTF'].calls
    assert ctrl._scanArmedLasers == ['561', '561AOTF']


def test_power_device_live_button_stays_off_when_scan_owns_emission():
    """Scan-owned emission must not masquerade as Live ON state."""
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._widget.active.get('561AOTF', False) is False


def test_entire_laser_widget_is_read_only_while_scan_is_active():
    ctrl = _controller(names=('561', '561AOTF'),
                       pairs={'561': '561AOTF'})

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._widget.editable is False
    LaserController.scanChanged(ctrl, False)
    assert ctrl._widget.editable is True


def test_a_laser_owning_both_gate_and_power_needs_no_pairing():
    """775AOM has its own analog channel and digital line."""
    ctrl = _controller(names=('775AOM',))

    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    assert ctrl._scanArmedLasers == ['775AOM']
    assert ctrl._master.lasersManager['775AOM'].calls[:2] == [
        ('value', 10.0), ('scanMode', True)
    ]


def test_a_missing_power_device_is_reported_and_does_not_block_the_gate():
    ctrl = _controller(names=('561',), pairs={'561': 'TypoAOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._scanArmedLasers == ['561']
    assert any(level == 'error' for level, _ in ctrl._logger.messages)


def test_direct_scan_power_is_applied_before_arming():
    ctrl = _controller(names=('561AOTF',))
    ctrl._livePowerValues['561AOTF'] = 10.0
    ctrl._scanPowerValues['561AOTF'] = 25.0

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    calls = ctrl._master.lasersManager['561AOTF'].calls
    assert calls[:2] == [('value', 25.0), ('scanMode', True)]

def test_laser_outside_the_ttl_list_is_untouched():
    ctrl = _controller(names=('561', '561AOTF', '640', '640AOTF'),
                       pairs={'561': '561AOTF', '640': '640AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['640'].calls == []
    assert ctrl._master.lasersManager['640AOTF'].calls == []
    assert '640' not in ctrl._widget.enableEditable


def test_arming_is_independent_of_the_live_toggle():
    """Ticking the box is the authority; the on/off button is not consulted."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls


@pytest.mark.parametrize(
    ('live_active', 'participating'),
    ((True, True), (False, True), (True, False), (False, False)),
)
def test_live_state_and_scan_participation_policy(live_active, participating):
    """Patch 3 changes participating paths only; nonparticipants stay baseline."""
    ctrl = _controller(names=('488',))
    ctrl._widget.active['488'] = live_active

    LaserController.scanDevicesResolved(
        ctrl, ['488'] if participating else []
    )

    calls = ctrl._master.lasersManager['488'].calls
    if participating:
        assert ('scanMode', True) in calls
        assert ctrl._scanArmedLasers == ['488']
    else:
        assert calls == []
        assert ctrl._scanArmedLasers == []

    if participating:
        # A participating Live-ON laser is darkened before scan ownership.
        assert ctrl._widget.active.get('488', False) is False
        if live_active:
            assert ('enabled', False) in calls
    else:
        # Immediate pre-work ImSwitch2 baseline: non-participants are untouched.
        assert ctrl._widget.active.get('488', False) is live_active
        assert calls == []


# --------------------------------------------------------------------------- #
# Waits between the parts of a multi-part run (timelapse timepoints)           #
# --------------------------------------------------------------------------- #

def test_power_device_pauses_when_a_lapse_part_ends():
    """The run stays armed through the wait (sigScanEnded is withheld for
    non-final parts), so a serially-enabled AOTF kept emitting into the sample
    for the whole waiting time."""
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    ctrl._master.lasersManager['561AOTF'].calls.clear()
    ctrl._master.lasersManager['561'].calls.clear()

    LaserController.scanPartDone(ctrl)

    assert ctrl._master.lasersManager['561AOTF'].calls == [('enabled', False)]
    assert ctrl._master.lasersManager['561'].calls == []
    assert ctrl._widget.active['561AOTF'] is False
    # Still the scan's laser: armed, with the whole LaserWidget read-only.
    assert ctrl._scanArmedLasers == ['561', '561AOTF']
    assert ctrl._widget.editable is False


def test_paused_power_device_resumes_when_the_next_part_arms():
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    LaserController.scanPartDone(ctrl)
    ctrl._master.lasersManager['561AOTF'].calls.clear()
    ctrl._master.lasersManager['561'].calls.clear()

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['561AOTF'].calls == [('enabled', True)]
    assert ctrl._widget.active.get('561AOTF', False) is False
    # The gate is not re-armed; membership republish is not a new run.
    assert ctrl._master.lasersManager['561'].calls == []


def test_repeat_frame_republish_does_not_toggle_the_power_device():
    """Repeat frames republish membership without a part terminal in between;
    they must stay free of per-frame RS232 writes."""
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['561AOTF'].calls == []


def test_pause_is_idempotent():
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanPartDone(ctrl)
    LaserController.scanPartDone(ctrl)

    assert ctrl._master.lasersManager['561AOTF'].calls == [('enabled', False)]


def test_gate_without_serial_power_device_is_not_paused_between_parts():
    """Pause only serial emission that arming explicitly switched on."""
    ctrl = _controller(names=('561AOTF',))
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanPartDone(ctrl)

    assert ctrl._master.lasersManager['561AOTF'].calls == []


def test_scan_done_outside_a_run_touches_nothing():
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})

    LaserController.scanPartDone(ctrl)

    assert ctrl._master.lasersManager['561AOTF'].calls == []
    assert ctrl._master.lasersManager['561'].calls == []


def test_teardown_clears_the_pause_so_a_new_run_starts_hot():
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    LaserController.scanPartDone(ctrl)
    LaserController.scanChanged(ctrl, False)
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ('enabled', True) in ctrl._master.lasersManager['561AOTF'].calls
    assert ctrl._scanArmedLasers == ['561', '561AOTF']


def test_stray_scan_done_after_teardown_is_ignored():
    """sigScanDone is global; a queued delivery can arrive after the run was
    torn down and must not switch anything off."""
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    LaserController.scanChanged(ctrl, False)
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanPartDone(ctrl)

    assert ctrl._master.lasersManager['561AOTF'].calls == []


# --------------------------------------------------------------------------- #
# Teardown touches only what this scan armed                                   #
# --------------------------------------------------------------------------- #

def test_armed_laser_ends_the_scan_off_with_the_ui_in_sync():
    ctrl = _controller(names=('775AOM',))
    LaserController.scanDevicesResolved(ctrl, ['775AOM'])
    ctrl._master.lasersManager['775AOM'].calls.clear()

    LaserController.scanChanged(ctrl, False)

    calls = ctrl._master.lasersManager['775AOM'].calls
    assert ('scanMode', False) in calls
    assert ('enabled', False) in calls
    assert ctrl._widget.active['775AOM'] is False


def test_teardown_leaves_nonparticipating_live_laser_untouched():
    ctrl = _controller(names=('561AOTF', '775AOM'))
    ctrl._widget.active['775AOM'] = True
    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])
    ctrl._master.lasersManager['775AOM'].calls.clear()

    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls == []
    assert ctrl._widget.active['775AOM'] is True

def test_teardown_restores_a_setpoint_that_leaving_scan_mode_destroyed():
    """NidaqLaserManager zeroes the output when it leaves scan mode, which is
    why an AOM went dark for every scan after the first."""
    ctrl = _controller(names=('775AOM',), zeroesOnExit=('775AOM',))
    ctrl._widget.values['775AOM'] = 20.0
    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls[-1] == ('value', 20.0)


def test_teardown_restores_explicit_zero_live_setpoint():
    ctrl = _controller(names=('561AOTF',))
    ctrl._livePowerValues['561AOTF'] = 0.0
    ctrl._scanPowerValues['561AOTF'] = 20.0
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['561AOTF'].calls[-1] == ('value', 0.0)

def test_teardown_is_idempotent():
    ctrl = _controller(names=('775AOM',))
    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    LaserController.scanChanged(ctrl, False)
    ctrl._master.lasersManager['775AOM'].calls.clear()
    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls == []


def test_consecutive_scans_leave_the_setpoint_intact():
    """The 'exactly one scan works, then dark' regression."""
    ctrl = _controller(names=('775AOM',), zeroesOnExit=('775AOM',))
    ctrl._widget.values['775AOM'] = 30.0

    for _ in range(3):
        LaserController.scanDevicesResolved(ctrl, ['775AOM'])
        LaserController.scanChanged(ctrl, False)
        assert ctrl._master.lasersManager['775AOM'].calls[-1] == ('value', 30.0)
        ctrl._master.lasersManager['775AOM'].calls.clear()


def test_arming_runs_once_per_run_not_per_repeat_frame():
    """Membership is republished every iteration and several managers issue
    blocking RS232 commands here, so re-arming per frame would stall a fast
    repeat scan."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])
    callsAfterFirst = list(ctrl._master.lasersManager['561AOTF'].calls)
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ctrl._master.lasersManager['561AOTF'].calls == callsAfterFirst


def test_a_new_run_arms_again_after_teardown():
    ctrl = _controller(names=('561AOTF',))
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])
    LaserController.scanChanged(ctrl, False)
    ctrl._master.lasersManager['561AOTF'].calls.clear()

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls


def test_arming_reports_gate_and_power_device():
    ctrl = _controller(names=('561', '561AOTF'),
                       pairs={'561': '561AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    logged = ' '.join(m for _, m in ctrl._logger.messages)
    assert '561' in logged and '561AOTF' in logged


def test_a_laser_that_cannot_be_armed_is_reported_as_not_emitting():
    ctrl = _controller(names=('561AOTF',))

    def explode(active):
        raise RuntimeError('serial timeout')

    ctrl._master.lasersManager['561AOTF'].setScanModeActive = explode
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ctrl._scanArmedLasers == []
    assert any(level == 'warning' for level, _ in ctrl._logger.messages)


def test_device_list_is_published_before_arming():
    """Arming after arm() would put the writes back inside the busy window."""
    source = inspect.getsource(SuperScanController._armScanIteration)
    publish = source.index('sigScanDevicesResolved')
    arm = source.index('_scanCoordinator.armWithStarter(')
    assert publish < arm


def test_participation_is_resolved_from_the_ttl_list_not_the_daq_channels():
    """Source guard: reverting to the AO/DO list makes AOTF lasers invisible
    again, which is the defect that started this."""
    from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager

    source = inspect.getsource(NidaqManager.resolveScanTTLDevices)
    assert "'TTLCycleSignalsDict'" in source
    assert 'getAnalogChannel' not in source
    assert 'getDigitalLine' not in source


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


# --------------------------------------------------------------------------- #
# Patch 3 Live/Scan handoff                                                    #
# --------------------------------------------------------------------------- #

def test_live_on_nonparticipant_remains_untouched_during_scan_handoff():
    ctrl = _controller(names=('405', '488'))
    ctrl._widget.active['405'] = True

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._master.lasersManager['405'].calls == []
    assert ctrl._widget.active['405'] is True
    assert ctrl._scanLiveEnabledSnapshot == {'488': False}


def test_live_on_participant_is_suppressed_before_scan_power_and_arming():
    ctrl = _controller(names=('488',))
    ctrl._widget.active['488'] = True
    ctrl._scanPowerValues['488'] = 20.0

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    calls = ctrl._master.lasersManager['488'].calls
    assert calls[:3] == [
        ('enabled', False), ('value', 20.0), ('scanMode', True)
    ]
    assert ctrl._widget.active['488'] is False
    assert ctrl._scanLiveEnabledSnapshot == {'488': True}


def test_explicit_zero_scan_power_is_written_before_scan_mode():
    ctrl = _controller(names=('488',))
    ctrl._livePowerValues['488'] = 5.0
    ctrl._scanPowerValues['488'] = 0.0

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._master.lasersManager['488'].calls[:2] == [
        ('value', 0.0), ('scanMode', True)
    ]


def test_ao_owned_laser_skips_direct_scan_power_write():
    ctrl = _controller(names=('488',))
    ctrl._livePowerValues['488'] = 5.0
    ctrl._scanPowerValues['488'] = 40.0
    ctrl._scanLaserState['488'] = LaserScanState(
        active_for_scan=True, power_control=LaserScanPowerControl.AO
    )

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ('value', 40.0) not in ctrl._master.lasersManager['488'].calls
    assert ('scanMode', True) in ctrl._master.lasersManager['488'].calls


def test_gate_scan_power_is_applied_to_power_device_once():
    ctrl = _controller(
        names=('561', '561AOTF'), pairs={'561': '561AOTF'}
    )
    ctrl._livePowerValues.update({'561': 1.0, '561AOTF': 2.0})
    ctrl._scanPowerValues.update({'561': 9.0, '561AOTF': 22.0})

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ('value', 22.0) in ctrl._master.lasersManager['561AOTF'].calls
    assert ('value', 9.0) not in ctrl._master.lasersManager['561'].calls


def test_repeat_membership_does_not_reapply_scan_power():
    ctrl = _controller(names=('488',))
    ctrl._scanPowerValues['488'] = 30.0

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    first = list(ctrl._master.lasersManager['488'].calls)
    LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._master.lasersManager['488'].calls == first


def test_scan_end_restores_live_power_only_after_off():
    ctrl = _controller(names=('488',))
    ctrl._livePowerValues['488'] = 3.0
    ctrl._scanPowerValues['488'] = 30.0
    ctrl._widget.active['488'] = True

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    ctrl._master.lasersManager['488'].calls.clear()
    LaserController.scanChanged(ctrl, False)

    calls = ctrl._master.lasersManager['488'].calls
    restore_index = max(i for i, call in enumerate(calls) if call == ('value', 3.0))
    off_indices = [i for i, call in enumerate(calls) if call == ('enabled', False)]
    assert off_indices and max(off_indices) < restore_index
    assert ctrl._widget.active['488'] is False
    assert ctrl._resumableLiveEnabledSnapshot == {'488': True}


def test_live_suppression_failure_sets_prearm_veto_and_scan_stays_unarmed():
    ctrl = _controller(names=('488',))
    ctrl._widget.active['488'] = True

    def fail_disable(enabled):
        if enabled is False:
            raise RuntimeError('USB timeout')

    ctrl._master.lasersManager['488'].setEnabled = fail_disable

    LaserController.scanChanged(ctrl, True)

    with pytest.raises(RuntimeError):
        LaserController.scanDevicesResolved(ctrl, ['488'])
    assert ctrl._scanPreparationFailed is not None
    assert ctrl._commChannel.scanWorkflow.error is not None
    assert ctrl._scanArmedLasers == []


def test_explicit_disable_rejection_sets_prearm_veto():
    ctrl = _controller(names=('488',))
    ctrl._widget.active['488'] = True

    def reject_disable(enabled):
        if enabled is False:
            return False
        return True

    ctrl._master.lasersManager['488'].setEnabled = reject_disable
    LaserController.scanChanged(ctrl, True)

    with pytest.raises(RuntimeError, match='suppress participating Live'):
        LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._commChannel.scanWorkflow.error is not None
    assert ctrl._scanArmedLasers == []


def test_scan_power_failure_sets_prearm_veto_before_manager_arming():
    ctrl = _controller(names=('488',))
    ctrl._scanPowerValues['488'] = 30.0

    def fail_value(value):
        raise RuntimeError('serial timeout')

    ctrl._master.lasersManager['488'].setValue = fail_value
    LaserController.scanChanged(ctrl, True)

    with pytest.raises(RuntimeError):
        LaserController.scanDevicesResolved(ctrl, ['488'])

    assert ctrl._commChannel.scanWorkflow.error is not None
    assert ctrl._scanArmedLasers == []


def test_abort_style_teardown_restores_owned_live_values_and_leaves_others_alone():
    ctrl = _controller(names=('405', '488'))
    ctrl._livePowerValues.update({'405': 1.0, '488': 2.0})
    ctrl._scanPowerValues['488'] = 20.0
    ctrl._widget.active.update({'405': True, '488': False})

    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    LaserController.scanChanged(ctrl, False)

    assert ctrl._widget.active == {'405': True, '488': False}
    assert ('value', 2.0) == ctrl._master.lasersManager['488'].calls[-1]
    assert ctrl._master.lasersManager['405'].calls == []
    assert ('enabled', False) in ctrl._master.lasersManager['488'].calls



def test_live_setpoint_restore_failure_discards_resume():
    ctrl = _controller(names=('488',))
    ctrl._livePowerValues['488'] = 3.0
    ctrl._scanPowerValues['488'] = 30.0
    ctrl._widget.active['488'] = True

    original_set_value = ctrl._master.lasersManager['488'].setValue

    def fail_only_live_restore(value):
        if value == 3.0:
            raise RuntimeError('restore timeout')
        return original_set_value(value)

    ctrl._master.lasersManager['488'].setValue = fail_only_live_restore
    LaserController.scanChanged(ctrl, True)
    LaserController.scanDevicesResolved(ctrl, ['488'])
    LaserController.scanChanged(ctrl, False)

    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False


def test_resume_live_uses_existing_enable_path_and_switches_to_live():
    ctrl = _controller(names=('405', '488', '640'))
    ctrl._resumableLiveEnabledSnapshot = {'405': True, '488': True}
    ctrl._widget.resumeAvailable = True
    ctrl._currentPowerContext = 'scan'
    calls = []

    def set_active(name, active):
        calls.append((name, active))
        ctrl._widget.active[name] = bool(active)

    contexts = []
    ctrl.setLaserActive = set_active
    ctrl.powerContextChanged = lambda context: contexts.append(getattr(context, 'value', context))

    assert LaserController.resumeLiveIllumination(ctrl) is True
    assert calls == [('405', True), ('488', True)]
    assert ctrl._widget.active.get('640', False) is False
    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False
    assert contexts == ['live']


def test_resume_live_is_invalidated_by_live_on_off_action_only():
    ctrl = _controller(names=('488',))
    ctrl._resumableLiveEnabledSnapshot = {'488': True}
    ctrl._widget.resumeAvailable = True

    LaserController.toggleLaser(ctrl, '488', False)

    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False


def test_live_power_edit_preserves_pending_resume():
    ctrl = _controller(names=('488',))
    ctrl._resumableLiveEnabledSnapshot = {'488': True}
    ctrl._widget.resumeAvailable = True

    LaserController.setLaserPowerSetting(ctrl, '488', 4.0, 'live')

    assert ctrl._resumableLiveEnabledSnapshot == {'488': True}
    assert ctrl._widget.resumeAvailable is True


def test_new_scan_clears_pending_resume():
    ctrl = _controller(names=('488',))
    ctrl._resumableLiveEnabledSnapshot = {'488': True}
    ctrl._widget.resumeAvailable = True

    LaserController.scanChanged(ctrl, True)

    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False


def test_resume_live_ignores_stale_laser_names_and_is_one_shot():
    ctrl = _controller(names=('488',))
    ctrl._resumableLiveEnabledSnapshot = {'missing': True, '488': True}
    ctrl._widget.resumeAvailable = True
    calls = []

    def set_active(name, active):
        calls.append((name, active))
        ctrl._widget.active[name] = bool(active)

    ctrl.setLaserActive = set_active
    ctrl.powerContextChanged = lambda context: None

    assert LaserController.resumeLiveIllumination(ctrl) is True
    assert calls == [('488', True)]
    assert ctrl._resumableLiveEnabledSnapshot is None
    assert LaserController.resumeLiveIllumination(ctrl) is False


def test_resume_live_rejection_leaves_laser_off_and_consumes_snapshot():
    ctrl = _controller(names=('488',))
    ctrl._resumableLiveEnabledSnapshot = {'488': True}
    ctrl._widget.resumeAvailable = True

    def reject(name, active):
        ctrl._widget.active[name] = False

    ctrl.setLaserActive = reject
    ctrl.powerContextChanged = lambda context: None

    assert LaserController.resumeLiveIllumination(ctrl) is False
    assert ctrl._widget.active['488'] is False
    assert ctrl._resumableLiveEnabledSnapshot is None
    assert ctrl._widget.resumeAvailable is False


def test_laser_mutations_are_rejected_while_scan_is_active():
    ctrl = _controller(names=('488',))
    ctrl.is_scanning = True

    mutators = (
        lambda: LaserController.toggleLaser(ctrl, '488', True),
        lambda: LaserController.setLaserPowerSetting(ctrl, '488', 5.0, 'live'),
        lambda: LaserController.setLaserPowerSetting(ctrl, '488', 25.0, 'scan'),
        lambda: LaserController.toggleModulation(ctrl, '488', True),
        lambda: LaserController.frequencyChanged(ctrl, '488', 100),
        lambda: LaserController.dutyCycleChanged(ctrl, '488', 50),
        lambda: LaserController.powerContextChanged(ctrl, 'scan'),
        lambda: LaserController.clearPresetSelection(ctrl),
        lambda: LaserController.savePresetAs(ctrl),
        lambda: LaserController.resumeLiveIllumination(ctrl),
    )
    for mutate in mutators:
        with pytest.raises(RuntimeError, match='read-only'):
            mutate()

    assert ctrl._master.lasersManager['488'].calls == []

def test_nidaq_prearm_boundary_enforces_laser_preparation_veto():
    source = inspect.getsource(SuperScanController._armScanIteration)
    checks = [
        pos for pos in range(len(source))
        if source.startswith('_raiseIfLaserScanPreparationFailed()', pos)
    ]
    publish = source.index('sigScanDevicesResolved')
    arm = source.index('_scanCoordinator.armWithStarter(')
    assert len(checks) >= 2
    assert checks[0] < publish < checks[-1] < arm


def test_triggerscope_prearm_boundary_enforces_laser_preparation_veto():
    source = inspect.getsource(TriggerScopeScanLifecycleMixin._startTriggerScopeScan)
    checks = [
        pos for pos in range(len(source))
        if source.startswith('_raiseIfLaserScanPreparationFailed()', pos)
    ]
    publish = source.index('sigScanDevicesResolved')
    firmware = source.index('def startFirmware')
    assert len(checks) >= 2
    assert checks[0] < publish < checks[-1] < firmware


def test_nidaq_membership_resolution_failure_is_mode_dependent_before_arm():
    source = inspect.getsource(SuperScanController._armScanIteration)
    resolve = source.index('resolveScanTTLDevices')
    refusal = source.index(
        'Could not resolve scan laser/device membership before arming.'
    )
    coupled_log = source.index(
        'membership consumers are not being notified'
    )
    arm = source.index('_scanCoordinator.armWithStarter(')
    assert 'separatedLaserPower' in source
    assert resolve < refusal < coupled_log < arm
    # Separated raises; coupled logs and falls through to normal arming.
    branch = source[resolve:arm]
    assert 'if separatedLaserPower:' in branch
    assert 'raise RuntimeError(' in branch


def test_event_triggered_membership_resolution_failure_is_mode_dependent():
    source = inspect.getsource(EventTriggeredControllerBase.runSlowScan)
    resolve = source.index('resolveScanTTLDevices')
    refusal = source.index('laser ownership is undefined')
    coupled_log = source.index('membership consumers are')
    trigger = source.index('_triggeredScanRunner.trigger')
    assert 'separatedLaserPower' in source
    assert resolve < refusal < coupled_log < trigger
    branch = source[resolve:trigger]
    assert 'if separatedLaserPower:' in branch
    assert 'return False' in branch
    # Coupled has no return in its logging branch and continues to trigger.
    coupled_branch = branch[branch.index('membership consumers are'): ]
    assert 'return False' not in coupled_branch.split('else:', 1)[0]


def test_coupled_laser_mutations_remain_allowed_while_scan_is_active():
    ctrl = _controller(names=('488', '640'), mode='coupled')
    ctrl.is_scanning = True
    ctrl._currentPowerContext = 'live'

    LaserController.toggleLaser(ctrl, '640', True)
    LaserController.setLaserPowerSetting(ctrl, '640', 5.0, 'live')

    assert ('enabled', True) in ctrl._master.lasersManager['640'].calls
    assert ('value', 5.0) in ctrl._master.lasersManager['640'].calls

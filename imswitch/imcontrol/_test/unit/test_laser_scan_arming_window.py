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
power device is switched on, both regardless of their manual toggles and both
off afterwards; their on/off buttons lock while the setpoints stay editable;
lasers outside the list are never touched.

Between the parts of a multi-part run (timelapse timepoints), the run stays
armed but the power devices the arming switched on are paused on each part's
``sigScanDone`` and switched back on when the next part republishes
membership — so the sample sits in darkness through the waits. Repeat frames
never publish a part terminal and are unaffected.
"""



import inspect

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.LaserController import (
    LaserController,
)


class _Laser:
    """``zeroesOnExit`` models NidaqLaserManager, whose setScanModeActive(False)
    is implemented as setValue(0) and so discards the user's setpoint."""

    def __init__(self, name, zeroesOnExit=False):
        self.name = name
        self.calls = []
        self._zeroesOnExit = zeroesOnExit

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

    def setLaserEnableEditable(self, name, editable):
        self.enableEditable[name] = editable

    def setLaserActive(self, name, active, emitSignal=True):
        self.active[name] = active

    def getValue(self, name):
        return self.values.get(name, 10.0)


class _LaserInfo:
    def __init__(self, powerDevice=None):
        self.powerDevice = powerDevice


class _SetupInfo:
    def __init__(self, lasers):
        self.lasers = lasers


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


def _controller(names=('561', '561AOTF'), pairs=None, zeroesOnExit=()):
    """``pairs`` maps a gate laser to the device that sets its power."""
    pairs = pairs or {}
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = _Master(_LasersManager(names, zeroesOnExit))
    ctrl._widget = _Widget()
    ctrl._logger = _Logger()
    ctrl._scanArmedLasers = []
    ctrl._scanEnabledLasers = []
    ctrl._scanLasersPaused = False
    ctrl._setupInfo = _SetupInfo(
        {name: _LaserInfo(pairs.get(name)) for name in names}
    )
    return ctrl


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


def test_power_device_button_is_synced_on_when_armed():
    """Locking the button while it reads OFF looks exactly like "I still have
    to press ON"."""
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._widget.active['561AOTF'] is True


def test_on_off_locks_for_both_while_setpoints_stay_editable():
    ctrl = _controller(names=('561', '561AOTF'),
                       pairs={'561': '561AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._widget.enableEditable == {'561': False, '561AOTF': False}
    assert not hasattr(ctrl._widget, 'editable')  # setpoints never locked


def test_a_laser_owning_both_gate_and_power_needs_no_pairing():
    """775AOM has its own analog channel and digital line."""
    ctrl = _controller(names=('775AOM',))

    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    assert ctrl._scanArmedLasers == ['775AOM']
    assert ctrl._master.lasersManager['775AOM'].calls == [('scanMode', True)]


def test_a_missing_power_device_is_reported_and_does_not_block_the_gate():
    ctrl = _controller(names=('561',), pairs={'561': 'TypoAOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._scanArmedLasers == ['561']
    assert any(level == 'error' for level, _ in ctrl._logger.messages)


def test_arming_never_writes_the_power():
    """The widget already pushed the setpoint to the device. Re-writing it at
    arm overwrote a good amplitude with a bad widget read and silenced the
    AOTF lasers, which armed "@0" while the widget showed a real power."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    calls = ctrl._master.lasersManager['561AOTF'].calls
    assert calls == [('scanMode', True)]


def test_laser_outside_the_ttl_list_is_untouched():
    ctrl = _controller(names=('561', '561AOTF', '640', '640AOTF'),
                       pairs={'561': '561AOTF', '640': '640AOTF'})

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['640'].calls == []
    assert ctrl._master.lasersManager['640AOTF'].calls == []
    assert '640' not in ctrl._widget.enableEditable


def test_arming_is_independent_of_the_manual_toggle():
    """Ticking the box is the authority; the on/off button is not consulted."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls


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
    # Still the scan's laser: armed, and the buttons stay locked.
    assert ctrl._scanArmedLasers == ['561', '561AOTF']
    assert ctrl._widget.enableEditable['561AOTF'] is False


def test_paused_power_device_resumes_when_the_next_part_arms():
    ctrl = _controller(names=('561', '561AOTF'), pairs={'561': '561AOTF'})
    LaserController.scanDevicesResolved(ctrl, ['561'])
    LaserController.scanPartDone(ctrl)
    ctrl._master.lasersManager['561AOTF'].calls.clear()
    ctrl._master.lasersManager['561'].calls.clear()

    LaserController.scanDevicesResolved(ctrl, ['561'])

    assert ctrl._master.lasersManager['561AOTF'].calls == [('enabled', True)]
    assert ctrl._widget.active['561AOTF'] is True
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


def test_a_manually_enabled_laser_is_not_paused():
    """The pause covers exactly the emission the arming switched on. A laser
    in the TTL list with no power pairing was enabled by hand (or gates over
    its own line) and keeps its manual state, just as it does at arming."""
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


def test_teardown_never_touches_a_laser_the_scan_did_not_arm():
    """Disarming every laser is what silently zeroed an unrelated AOM."""
    ctrl = _controller(names=('561AOTF', '775AOM'))
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])
    ctrl._master.lasersManager['775AOM'].calls.clear()

    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls == []
    assert '775AOM' not in ctrl._widget.active


def test_teardown_restores_a_setpoint_that_leaving_scan_mode_destroyed():
    """NidaqLaserManager zeroes the output when it leaves scan mode, which is
    why an AOM went dark for every scan after the first."""
    ctrl = _controller(names=('775AOM',), zeroesOnExit=('775AOM',))
    ctrl._widget.values['775AOM'] = 20.0
    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls[-1] == ('value', 20.0)


def test_teardown_never_writes_a_zero_setpoint():
    """A widget reading 0 for a laser the user did set (the AOTFs) must not
    have its real amplitude destroyed on the way out."""
    ctrl = _controller(names=('561AOTF',))
    ctrl._widget.values['561AOTF'] = 0.0
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    LaserController.scanChanged(ctrl, False)

    assert ('value', 0.0) not in ctrl._master.lasersManager['561AOTF'].calls


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
    arm = source.index('_scanCoordinator.arm(')
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

"""Laser scan participation and arming (rig findings).

Two independent, long-standing defects, both visible on a STED setup with
AOTF-gated and NI-DAQ lasers:

* Participation was decided from the NI-DAQ AO/DO device list, which only
  contains devices owning an analog channel or digital line. An AOTF-gated
  laser has neither, so it never appeared there: never greyed out, and never
  handed over to external control. It emitted only if the user happened to
  switch it on by hand. Participation now comes from the scan's TTL device
  list — the rows the user actually ticks.
* Arming inherited whatever amplitude the previous manual action left behind,
  while scan teardown called ``setScanModeActive(False)`` on EVERY laser.
  On a manager with an analog channel that zeroes the output, so an AOM was
  silenced at the end of the first scan and stayed dark for every scan after
  it. Arming now applies the widget setpoint explicitly, and teardown touches
  only the lasers this scan armed.

Contract: a laser in the scan's TTL list is armed and gated by the scan
regardless of its manual toggle, and ends the scan off; a laser outside it is
never touched.
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
        self.editable = {}
        self.active = {}
        self.values = {}

    def setLaserEditable(self, name, editable):
        self.editable[name] = editable

    def setLaserActive(self, name, active, emitSignal=True):
        self.active[name] = active

    def getValue(self, name):
        return self.values.get(name, 10.0)


class _LaserInfo:
    def __init__(self, digitalLine=None, analogChannel=None):
        self._digitalLine = digitalLine
        self._analogChannel = analogChannel

    def getDigitalLine(self):
        return self._digitalLine

    def getAnalogChannel(self):
        return self._analogChannel


class _SetupInfo:
    def __init__(self, lasers):
        self.lasers = lasers


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(('info', message))

    def warning(self, message, *args, **kwargs):
        self.messages.append(('warning', message))

    def error(self, message, *args, **kwargs):
        self.messages.append(('error', message))


def _controller(names=('L488', 'L561'), scanChannels=True, zeroesOnExit=()):
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = _Master(_LasersManager(names, zeroesOnExit))
    ctrl._widget = _Widget()
    ctrl._logger = _Logger()
    ctrl._scanArmedLasers = []
    ctrl._setupInfo = _SetupInfo({
        name: _LaserInfo(digitalLine='Dev1/port0/line1' if scanChannels else None)
        for name in names
    })
    return ctrl


# --------------------------------------------------------------------------- #
# Participation comes from the TTL list                                        #
# --------------------------------------------------------------------------- #

def test_ttl_programmed_laser_is_armed_and_greyed_out():
    """Includes the AOTF case: no analog channel, no digital line, yet it is
    in the scan's TTL list and must be armed."""
    ctrl = _controller(names=('561AOTF', '775AOM'), scanChannels=False)

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls
    assert ctrl._widget.editable['561AOTF'] is False


def test_arming_never_writes_the_power():
    """The widget already pushed the setpoint to the device. Re-writing it at
    arm overwrote a good amplitude with a bad widget read and silenced the
    AOTF lasers, which armed "@0" while the widget showed a real power."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    calls = ctrl._master.lasersManager['561AOTF'].calls
    assert calls == [('scanMode', True)]


def test_laser_outside_the_ttl_list_is_untouched_and_editable():
    ctrl = _controller(names=('561AOTF', '775AOM'))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ctrl._master.lasersManager['775AOM'].calls == []
    assert ctrl._widget.editable['775AOM'] is True


def test_arming_is_independent_of_the_manual_toggle():
    """Ticking the box is the authority; the on/off button is not consulted."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls


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


def test_arming_reports_which_lasers_will_emit():
    """The un-armed AOTF was invisible: the scan ran, the UI looked fine, and
    the sample saw no light."""
    ctrl = _controller(names=('561AOTF', '775AOM'))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    logged = ' '.join(m for _, m in ctrl._logger.messages)
    assert '561AOTF' in logged and '775AOM' in logged


def test_a_laser_that_cannot_be_armed_is_reported_as_not_emitting():
    ctrl = _controller(names=('561AOTF',))

    def explode(active):
        raise RuntimeError('serial timeout')

    ctrl._master.lasersManager['561AOTF'].setScanModeActive = explode
    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ctrl._scanArmedLasers == []
    assert any(level == 'warning' for level, _ in ctrl._logger.messages)


def test_scan_built_stays_ui_only():
    """It fires inside the DAQ-busy window; hardware writes there are refused."""
    ctrl = _controller(names=('775AOM',))

    LaserController.scanBuilt(ctrl, ['775AOM'])

    assert ctrl._master.lasersManager['775AOM'].calls == []


# --------------------------------------------------------------------------- #
# The publication window itself                                                #
# --------------------------------------------------------------------------- #

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

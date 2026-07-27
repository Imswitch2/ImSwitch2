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
    def __init__(self, name):
        self.name = name
        self.calls = []

    def setScanModeActive(self, active):
        self.calls.append(('scanMode', active))

    def setEnabled(self, enabled):
        self.calls.append(('enabled', enabled))

    def setValue(self, value):
        # AAAOTFLaserManager and most other managers define exactly this
        # signature; passing enabled/for_scanning keywords raises TypeError.
        self.calls.append(('value', value))


class _LasersManager:
    def __init__(self, names):
        self._lasers = {name: _Laser(name) for name in names}

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


def _controller(names=('L488', 'L561'), scanChannels=True):
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = _Master(_LasersManager(names))
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


def test_arming_applies_the_widget_setpoint_explicitly():
    """Without this an AOM whose amplitude was zeroed by a previous scan's
    teardown stays dark for every subsequent scan."""
    ctrl = _controller(names=('775AOM',))
    ctrl._widget.values['775AOM'] = 42.0

    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    assert ('value', 42.0) in ctrl._master.lasersManager['775AOM'].calls


def test_arming_uses_the_single_argument_setValue_every_manager_defines():
    """AAAOTFLaserManager is setValue(power). Passing the base class's
    optional keywords raises TypeError there and the laser is skipped as
    'failed to arm' — which is silent breakage for most hardware."""
    ctrl = _controller(names=('561AOTF',))

    LaserController.scanDevicesResolved(ctrl, ['561AOTF'])

    assert ctrl._scanArmedLasers == ['561AOTF']
    assert ('scanMode', True) in ctrl._master.lasersManager['561AOTF'].calls


def test_amplitude_is_applied_before_the_handover():
    ctrl = _controller(names=('775AOM',))

    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    calls = [name for name, _ in ctrl._master.lasersManager['775AOM'].calls]
    assert calls.index('value') < calls.index('scanMode')


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


def test_teardown_is_idempotent():
    ctrl = _controller(names=('775AOM',))
    LaserController.scanDevicesResolved(ctrl, ['775AOM'])

    LaserController.scanChanged(ctrl, False)
    ctrl._master.lasersManager['775AOM'].calls.clear()
    LaserController.scanChanged(ctrl, False)

    assert ctrl._master.lasersManager['775AOM'].calls == []


def test_consecutive_scans_each_reapply_the_amplitude():
    """The 'exactly one scan works, then dark' regression."""
    ctrl = _controller(names=('775AOM',))
    ctrl._widget.values['775AOM'] = 30.0

    for _ in range(3):
        LaserController.scanDevicesResolved(ctrl, ['775AOM'])
        applied = [v for n, v in ctrl._master.lasersManager['775AOM'].calls
                   if n == 'value']
        assert applied[-1] == 30.0
        LaserController.scanChanged(ctrl, False)


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

    def explode(power):
        raise RuntimeError('serial timeout')

    ctrl._master.lasersManager['561AOTF'].setValue = explode
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

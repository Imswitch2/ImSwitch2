"""Laser arming happens while the NI-DAQ is still free (rig finding).

``LaserController`` armed lasers from ``sigScanBuilt``, which ``runScan``
emits *after* marking the NI-DAQ manager busy. Every one-shot digital/analog
write it issued there was therefore refused — silently before the DAQ manager
was hardened, and afterwards as
``cannot start Ni-daq task setDigitalTask, the manager is busy``.

The consequence was not cosmetic: a laser outside the scan's device list was
never actually forced off, so a laser left on for live view stayed on for the
whole scan.

Arming now runs on ``sigScanDevicesResolved``, published before the scan
claims the DAQ. ``sigScanBuilt`` keeps only UI editability.

Design reference: docs/design/plans/detector-acquisition-selection.md.
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

    def setLaserEditable(self, name, editable):
        self.editable[name] = editable

    def setLaserActive(self, name, active, emitSignal=True):
        self.active[name] = active


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


def _controller(names=('L488', 'L561'), scanChannels=True):
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = _Master(_LasersManager(names))
    ctrl._widget = _Widget()
    ctrl._scanBuiltApplied = False
    ctrl._setupInfo = _SetupInfo({
        name: _LaserInfo(digitalLine='Dev1/port0/line1' if scanChannels else None)
        for name in names
    })
    return ctrl


# --------------------------------------------------------------------------- #
# Arming moved off sigScanBuilt                                                #
# --------------------------------------------------------------------------- #

def test_scan_built_does_no_hardware_work():
    """The whole point: no DAQ writes from a handler that runs while busy."""
    ctrl = _controller()

    LaserController.scanBuilt(ctrl, ['L488'])

    for laser in ('L488', 'L561'):
        assert ctrl._master.lasersManager[laser].calls == []


def test_scan_built_still_updates_editability():
    ctrl = _controller()

    LaserController.scanBuilt(ctrl, ['L488'])

    assert ctrl._widget.editable == {'L488': False, 'L561': True}


def test_participating_laser_is_armed_before_the_scan_claims_the_daq():
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])

    assert ('scanMode', True) in ctrl._master.lasersManager['L488'].calls


def test_non_participating_laser_with_a_scan_channel_is_forced_off():
    """The bug that mattered: this laser used to stay on for the whole scan."""
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])

    calls = ctrl._master.lasersManager['L561'].calls
    assert ('scanMode', False) in calls
    assert ('enabled', False) in calls
    assert ctrl._widget.active['L561'] is False


def test_laser_without_a_scan_channel_is_left_alone():
    """A pure RS232 laser cannot be gated by the scan, so it keeps user state."""
    ctrl = _controller(scanChannels=False)

    LaserController.scanDevicesResolved(ctrl, ['L488'])

    assert ctrl._master.lasersManager['L561'].calls == []


def test_arming_runs_once_per_scan_sequence_not_per_repeat_frame():
    """Membership is republished every iteration; blocking serial commands
    must not be re-issued on every repeat frame."""
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])
    callsAfterFirst = list(ctrl._master.lasersManager['L561'].calls)
    LaserController.scanDevicesResolved(ctrl, ['L488'])

    assert ctrl._master.lasersManager['L561'].calls == callsAfterFirst


# --------------------------------------------------------------------------- #
# The publication window itself                                                #
# --------------------------------------------------------------------------- #

def test_device_list_is_published_before_arming():
    """Source guard: publishing after arm() would put consumers back inside
    the busy window, which is the whole defect."""
    source = inspect.getsource(SuperScanController._armScanIteration)
    publish = source.index('sigScanDevicesResolved')
    arm = source.index('_scanCoordinator.arm(')
    assert publish < arm


def test_resolved_devices_match_what_run_scan_would_drive():
    """The published list must be the same one runScan builds its tasks from;
    otherwise lasers are armed against a membership the scan does not honour."""
    from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager

    source = inspect.getsource(NidaqManager.resolveScanDevices)
    assert "'scanSignalsDict'" in source
    assert "'TTLCycleSignalsDict'" in source
    assert 'getAnalogChannel' in source
    assert 'getDigitalLine' in source


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

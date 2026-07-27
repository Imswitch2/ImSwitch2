"""Laser arming happens while the NI-DAQ is still free (rig finding).

``LaserController`` armed lasers from ``sigScanBuilt``, which ``runScan``
emits *after* marking the NI-DAQ manager busy, so every one-shot write it
issued there was refused — silently at first, later as
``cannot start Ni-daq task setDigitalTask, the manager is busy``.

Moving that work earlier made the writes real for the first time, and on
hardware it went wrong: lasers were driven low at scan start and never
restored, so scans ran dark and the laser stayed off afterwards while the UI
still showed it on.

The scan drives the TTL lines it owns through its own DO task and does not
need the static line pre-dropped; lasers the scan does not own must keep the
state the user set. So the scan path performs NO laser hardware writes, which
is what the system did in practice all along. Membership is still published
early (``sigScanDevicesResolved``) for UI editability.

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
# No hardware writes on the scan path                                          #
# --------------------------------------------------------------------------- #

def test_scan_built_does_no_hardware_work():
    ctrl = _controller()

    LaserController.scanBuilt(ctrl, ['L488'])

    for laser in ('L488', 'L561'):
        assert ctrl._master.lasersManager[laser].calls == []


def test_scan_membership_does_no_hardware_work():
    """The rig regression: driving lines here left lasers dark for the scan
    and never restored them. The scan owns the TTL lines it drives; lasers it
    does not own keep whatever the user set."""
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])

    for laser in ('L488', 'L561'):
        assert ctrl._master.lasersManager[laser].calls == []


def test_a_manually_enabled_laser_outside_the_scan_is_left_on():
    """Forcing it off removed illumination the user deliberately enabled."""
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])

    assert 'L561' not in ctrl._widget.active
    assert ctrl._master.lasersManager['L561'].calls == []


def test_both_handlers_update_editability():
    ctrl = _controller()

    LaserController.scanDevicesResolved(ctrl, ['L488'])
    assert ctrl._widget.editable == {'L488': False, 'L561': True}

    ctrl._widget.editable.clear()
    LaserController.scanBuilt(ctrl, ['L488'])
    assert ctrl._widget.editable == {'L488': False, 'L561': True}


def test_no_laser_manager_call_appears_on_the_scan_path_at_all():
    """Source guard: any setEnabled/setScanModeActive/setValue reintroduced
    here runs against hardware at scan arm, which is what broke the rig."""
    for handler in (LaserController.scanDevicesResolved,
                    LaserController.scanBuilt):
        source = inspect.getsource(handler)
        body = source.partition('"""')[2].partition('"""')[2]
        for forbidden in ('setEnabled(', 'setScanModeActive(', 'setValue('):
            assert forbidden not in body, (handler.__name__, forbidden)


# --------------------------------------------------------------------------- #
# The publication window itself                                                #
# --------------------------------------------------------------------------- #

def test_device_list_is_published_before_arming():
    """Membership must still reach consumers before the DAQ is claimed."""
    source = inspect.getsource(SuperScanController._armScanIteration)
    publish = source.index('sigScanDevicesResolved')
    arm = source.index('_scanCoordinator.arm(')
    assert publish < arm


def test_resolved_devices_match_what_run_scan_would_drive():
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

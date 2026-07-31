"""NI-DAQ busy refusal is observable (Phase 3, R7-1).

``NidaqManager.runScan`` used to silently no-op when ``self.busy`` — no
exception, no ``sigScanBuildFailed``, no ``sigScanBuilt``. The caller was left
believing it had armed a scan that never started: the scan controller stayed
``isRunning`` with its run button latched forever, and (once acquisition leases
exist) its SCAN lease and lifecycle token would never be resolved, permanently
locking detector selection.

It now raises ``ScanBusyError``, which callers unwind like any other arm
failure. This is the fifth exactly-once completion case in the plan: *scan
never started, no failure signal*.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcommon.framework import SignalInterface
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager, NidaqManagerError, ScanBusyError,
)


class _Recorder:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _busyManager():
    """A NidaqManager with __init__ bypassed, parked in the busy state."""
    manager = NidaqManager.__new__(NidaqManager)
    SignalInterface.__init__(manager)  # else __del__ raises at GC time
    manager._NidaqManager__scanSimulator = None
    manager.doTaskWaiter = None
    manager.aoTaskWaiter = None
    manager.timerTaskWaiter = None
    manager.busy = True
    manager.signalSent = False
    manager.tasks = {}
    manager.sigScanBuilt = _Recorder()
    manager.sigScanStarted = _Recorder()
    manager.sigScanBuildFailed = _Recorder()
    return manager


def test_run_scan_while_busy_raises_instead_of_silently_no_opping():
    manager = _busyManager()

    with pytest.raises(ScanBusyError):
        manager.runScan({'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}}, {})


def test_busy_refusal_arms_nothing_and_emits_no_lifecycle_signal():
    """The caller must be able to tell refusal from a started scan: nothing
    was armed, so no scan-lifecycle signal may fire and `busy` must be left
    as it was (the in-flight scan still owns it)."""
    manager = _busyManager()

    with pytest.raises(ScanBusyError):
        manager.runScan({'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}}, {})

    assert manager.sigScanBuilt.emitted == []
    assert manager.sigScanStarted.emitted == []
    assert manager.sigScanBuildFailed.emitted == []
    assert manager.busy is True  # the running scan's flag, not ours to clear
    assert manager.tasks == {}


def test_scan_busy_error_is_a_nidaq_manager_error():
    """Callers with a broad `except NidaqManagerError` keep working."""
    assert issubclass(ScanBusyError, NidaqManagerError)


def test_scan_busy_error_message_survives_str():
    """NidaqManagerError never called super().__init__, so str(exc) was ''
    and refusals would have been logged as empty strings."""
    error = ScanBusyError('daq is busy')

    assert str(error) == 'daq is busy'
    assert error.message == 'daq is busy'


def test_etsted_runner_reports_busy_refusal_as_a_failed_trigger():
    """The 5th runScan entry point must not let the refusal escape into the
    Qt event loop — it returns its own result type."""
    from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import (
        EtSTEDTriggeredScanRunner,
    )

    class _BusyCoordinator:
        def arm(self, signalDic, scanInfoDict, owner=None):
            raise ScanBusyError('daq is busy')

    runner = EtSTEDTriggeredScanRunner()
    result = runner.trigger(
        runner.scan_widget_mode,
        signal_dict={'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}},
        scan_info_dict={},
        scan_coordinator=_BusyCoordinator(),
        scan_owner=runner,
    )

    assert result.success is False
    assert 'busy' in result.message


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

"""TriggerScope scan-start boundary and abort (rig findings).

Two defects, both rooted in the fact that the TriggerScope firmware runs a
scan autonomously once commanded and answers only with ``"Scan done"``.

**The scan boundary was announced too late.** Every handler sent its
``*_SCAN`` command and only then emitted ``sigScanStarted``. That signal is
what consumers use as the "frames from here on belong to this scan" boundary
-- BeadRec registers its detector chunk consumer in that slot, and
``startChunkConsumer`` excludes every frame captured before it. So the frames
the firmware had already triggered in the gap were dropped and the
reconstruction came out shifted by a few pixels.

Announcing at ``sigScanStarting`` instead would invert the bug rather than
fix it: that signal fires before the parameter upload, and ``setParameter``
sleeps 50 ms per parameter, putting it roughly a second ahead of the scan
command -- a second of free-running camera backlog pulled into the
reconstruction. The boundary belongs immediately before the firmware command,
where the firmware is provably still idle because it has not been told to
scan yet.

**Abort could not stop a repeat.** ``abortScan`` acted only when no scan was
running, so an abort raised mid-scan -- the RecordingController raises one
whenever a scan-driven recording stops -- was swallowed. With Repeat ticked
the scanner simply armed the next iteration and kept cycling. The firmware
still cannot be interrupted, so the first stop lets the iteration under way
finish and denies the repeat. A deliberate second-stage UI action can force
local teardown and laser disarm if the firmware never reports completion.
"""

from collections import defaultdict
import threading
from types import SimpleNamespace

from imswitch.imcontrol.controller.controllers.TriggerScopeRasterController import (
    TriggerScopeRasterController,
)
from imswitch.imcontrol.model.managers.ScanManagerTriggerScope import (
    ScanManagerTriggerScope,
)
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)
from imswitch.imcontrol.view.widgets.TriggerScopeRasterWidget import (
    TriggerScopeRasterWidget,
)


# --------------------------------------------------------------------------- #
# The boundary precedes the firmware command                                   #
# --------------------------------------------------------------------------- #

class _AnyDevice(dict):
    """deviceInfo for a board where every name resolves to some channel."""

    def __missing__(self, key):
        return {'TTLLine': '1', 'DACChannel': '2', 'MinV': -10, 'MaxV': 10}


class _Signal:
    def __init__(self, log):
        self._log = log

    def emit(self, *args):
        self._log.append('boundary')


class _TriggerScope:
    """Records the interleaving of the boundary signal and serial traffic."""

    def __init__(self):
        self.events = []
        self.deviceInfo = _AnyDevice()
        self.sigScanStarted = _Signal(self.events)

    def setParameter(self, name, value):
        self.events.append(f'param:{name}')

    def send(self, command):
        self.events.append(f'send:{command}')


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _scanManager():
    manager = ScanManagerTriggerScope.__new__(ScanManagerTriggerScope)
    triggerScope = _TriggerScope()
    manager._ts = triggerScope
    manager._logger = _Logger()
    return manager, triggerScope


_RASTER_PARAMS = {
    'Digital': {
        'sequence_time': 0.001,
        'TTL_start': [0.0],
        'TTL_end': [0.0005],
        'target_device': ['CAM'],
    },
    'Analog': {
        'targets': ['X', 'Y'],
        'startPos': [0.0, 0.0],
        'lengths': [1.0, 1.0],
        'stepSizes': [0.1, 0.1],
    },
}


def _sequenceParams():
    return {
        'deviceParameters': defaultdict(lambda: 'device'),
        'scanParameters': {'someParameter': 1},
    }


_SCAN_TYPES = {
    'rasterScan': _RASTER_PARAMS,
    'pLS-RESOLFTScan': None,
    'GalvoDetectionScan': None,
    'MulticolorScan': None,
    'pLS-RESOLFT_multicolor_Scan': None,
    'LSXYRScan': None,
}


def test_raster_announces_the_boundary_before_commanding_the_firmware():
    """The reported case: BeadRec's chunk consumer must be registered before
    the firmware can trigger the camera, or the first frames are discarded and
    the reconstruction is shifted."""
    manager, triggerScope = _scanManager()

    manager.runScan(_RASTER_PARAMS, 'rasterScan')

    assert (triggerScope.events.index('boundary')
            < triggerScope.events.index('send:RASTER_SCAN'))


def test_every_scan_type_announces_the_boundary_before_its_command():
    """All six share one firmware and one consumer set; a boundary that is
    correct only for raster leaves the other five shifted."""
    for scanType, rasterParams in _SCAN_TYPES.items():
        manager, triggerScope = _scanManager()

        manager.runScan(rasterParams or _sequenceParams(), scanType)

        commands = [e for e in triggerScope.events if e.startswith('send:')]
        assert len(commands) == 1, scanType
        assert (triggerScope.events.index('boundary')
                < triggerScope.events.index(commands[0])), scanType


def test_the_boundary_follows_the_whole_parameter_upload():
    """It must not move all the way back to sigScanStarting territory: every
    PARAMETER write sleeps 50 ms, so a boundary before them trails the scan
    command by ~1 s and sweeps a free-running camera's backlog into the scan."""
    manager, triggerScope = _scanManager()

    manager.runScan(_RASTER_PARAMS, 'rasterScan')

    lastParameter = max(i for i, e in enumerate(triggerScope.events)
                        if e.startswith('param:'))
    assert triggerScope.events.index('boundary') > lastParameter


def test_an_unknown_scan_type_commands_nothing():
    manager, triggerScope = _scanManager()

    manager.runScan(_RASTER_PARAMS, 'noSuchScan')

    assert triggerScope.events == []


# --------------------------------------------------------------------------- #
# Abort denies the repeat                                                      #
# --------------------------------------------------------------------------- #

class _CommSignal:
    def __init__(self, name, log):
        self._name = name
        self._log = log

    def emit(self, *args):
        self._log.append(self._name)


class _CommChannel:
    def __init__(self):
        self.emitted = []
        self.activeScanSource = None
        self.sigScanDone = _CommSignal('scanDone', self.emitted)
        self.sigScanEnded = _CommSignal('scanEnded', self.emitted)

    def setActiveScanSource(self, source):
        self.activeScanSource = source

    def clearActiveScanSource(self, source):
        if self.activeScanSource is source:
            self.activeScanSource = None


class _RasterWidget:
    def __init__(self, repeat):
        self._repeat = repeat
        self.buttonChecked = None
        self.abortPending = False

    def repeatEnabled(self):
        return self._repeat

    def setScanButtonChecked(self, checked):
        self.buttonChecked = checked
        if not checked:
            self.abortPending = False

    def setAbortPending(self, pending):
        self.abortPending = pending


class _NoDetectors:
    def getAllDeviceNames(self, condition=None):
        return []

    def isDetectorFaulted(self, name):
        return False


def _rasterController(*, repeat, running=True):
    ctrl = TriggerScopeRasterController.__new__(TriggerScopeRasterController)
    ctrl._logger = _Logger()
    ctrl._commChannel = _CommChannel()
    ctrl._widget = _RasterWidget(repeat)
    ctrl.doingNonFinalPartOfSequence = False
    ctrl._scanStopRequested = False
    ctrl.reruns = []
    ctrl.runScanAdvanced = (
        lambda **kwargs: ctrl.reruns.append(kwargs)
    )
    ctrl._scanCoordinator = ScanExecutionCoordinator(
        _NoDetectors(), SimpleNamespace()
    )
    ctrl._triggerScopeRunToken = None
    ctrl._triggerScopeStartingPublished = False
    ctrl._triggerScopeRepeatPending = False
    ctrl._triggerScopeCompletionPublishing = False
    ctrl._triggerScopeTerminalLock = threading.RLock()

    def startIteration():
        runToken = ctrl._scanCoordinator.reserveRun(ctrl)
        ctrl._triggerScopeRunToken = runToken
        ctrl._triggerScopeStartingPublished = True
        ctrl._scanStopRequested = False
        ctrl._scanCoordinator.armWithStarter(
            lambda: None, owner=ctrl
        )
        ctrl.isRunning = True

    ctrl.startTestIteration = startIteration
    if running:
        startIteration()
    else:
        ctrl.isRunning = False
    return ctrl


def test_abort_mid_scan_stops_the_repeat_loop():
    """The defect: a recording stop raises sigAbortScan, which did nothing,
    and the scanner cycled on with the recording already gone."""
    ctrl = _rasterController(repeat=True)

    ctrl.abortScan()
    assert ctrl._widget.abortPending is True
    ctrl.scanDone()

    assert ctrl.reruns == []
    assert 'scanEnded' in ctrl._commChannel.emitted
    assert ctrl._widget.buttonChecked is False


def test_the_current_iteration_still_completes_normally():
    """The firmware cannot be interrupted, so an abort must not fake a
    failure: the iteration under way finishes and reports scan-done."""
    ctrl = _rasterController(repeat=True)

    ctrl.abortScan()
    ctrl.scanDone()

    assert ctrl._commChannel.emitted == ['scanDone', 'scanEnded']


def test_repeat_still_repeats_when_no_abort_was_raised():
    ctrl = _rasterController(repeat=True)

    ctrl.scanDone()
    ctrl._fireTriggerScopeRepeat()

    assert ctrl.reruns == [{'sigScanStartingEmitted': True}]
    assert 'scanEnded' not in ctrl._commChannel.emitted


def test_an_abort_does_not_leak_into_the_next_run():
    """The abort applies to the run it interrupted, not to the checkbox: the
    next Run Scan must repeat again."""
    ctrl = _rasterController(repeat=True)
    ctrl.abortScan()
    ctrl.scanDone()

    ctrl.startTestIteration()
    ctrl.scanDone()
    ctrl._fireTriggerScopeRepeat()

    assert ctrl.reruns == [{'sigScanStartingEmitted': True}]


def test_abort_while_idle_only_unsticks_its_own_ui():
    """An idle controller must not emit a global end for another scan owner."""
    ctrl = _rasterController(repeat=True, running=False)

    ctrl.abortScan()

    assert ctrl._widget.buttonChecked is False
    assert ctrl._commChannel.emitted == []


def test_a_failure_clears_a_pending_abort():
    ctrl = _rasterController(repeat=True)
    ctrl.abortScan()

    ctrl.scanFailed()

    assert ctrl._scanStopRequested is False
    assert ctrl._widget.abortPending is False


def test_duplicate_automatic_aborts_do_not_force_teardown():
    """Recording and workflow teardown may both emit sigAbortScan. Only the
    dedicated operator action is allowed to give up waiting for firmware."""
    ctrl = _rasterController(repeat=True)

    ctrl.abortScan()
    ctrl.abortScan()

    assert ctrl.isRunning is True
    assert ctrl._scanStopRequested is True
    assert ctrl._commChannel.emitted == []


def test_operator_can_force_teardown_after_requesting_a_stop():
    ctrl = _rasterController(repeat=True)

    ctrl.abortScan()
    ctrl.forceStopScan()

    assert ctrl.isRunning is False
    assert ctrl._scanStopRequested is False
    assert ctrl._widget.buttonChecked is False
    assert ctrl._commChannel.emitted == ['scanEnded']


def test_force_stop_requires_an_ordinary_stop_first():
    ctrl = _rasterController(repeat=True)

    ctrl.forceStopScan()

    assert ctrl.isRunning is True
    assert ctrl._commChannel.emitted == []


def test_late_scan_done_after_forced_teardown_is_ignored():
    ctrl = _rasterController(repeat=True)
    ctrl.abortScan()
    ctrl.forceStopScan()

    ctrl.scanDone()

    assert ctrl.reruns == []
    assert ctrl._commChannel.emitted == ['scanEnded']


def test_raster_stop_button_requires_a_deliberate_second_stage(qtbot):
    widget = TriggerScopeRasterWidget(SimpleNamespace())
    qtbot.addWidget(widget)
    actions = []
    widget.sigAbortScanClicked.connect(lambda: actions.append('stop'))
    widget.sigForceStopScanClicked.connect(lambda: actions.append('force'))
    widget.setScanButtonChecked(True)

    widget.abortScanBtn.click()
    assert actions == ['stop']

    # The controller moves the button into this state only after accepting
    # the ordinary stop request.
    widget.setAbortPending(True)
    widget.abortScanBtn.click()
    assert actions == ['stop', 'force']

    widget.setScanButtonChecked(False)
    assert widget.abortScanBtn.isEnabled() is False
    assert widget.abortScanBtn.text() == 'Stop after scan'


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

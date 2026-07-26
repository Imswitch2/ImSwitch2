"""Event-modality detector ownership corner cases (review round 8).

Two ways to get this wrong, both of which silently stall or leak rather than
failing loudly:

* EtSnouty has two clock modes that consume frames differently. Its
  ``ClockWidefield`` mode reads frames itself (EVENT_DIRECT); the other mode
  subscribes to ``sigUpdateImage`` and therefore needs to be in the
  frame-stream membership (EVENT_STREAM). Leasing EVENT_DIRECT for the
  signal-driven mode arms the detector but never starts the poller, so the
  mode stalls whenever live view is off.
* ``pauseFastModality`` drops the image-signal connection but deliberately
  KEEPS the lease. A release guarded on that connection flag therefore skips a
  paused run and leaks the handle for the rest of the session.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

from imswitch.imcontrol.controller.controllers.EtSnoutyController import (
    EtSnoutyController,
)
from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


class _RecordingDetectorsManager:
    def __init__(self):
        self.acquired = []
        self.released = []
        self._seq = 0

    def acquire(self, detectorNames, purpose):
        self._seq += 1
        handle = f'handle-{self._seq}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


class _Master:
    def __init__(self, detectorsManager):
        self.detectorsManager = detectorsManager


class _Logger:
    def error(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


# --------------------------------------------------------------------------- #
# EtSnouty: purpose follows the clock mode                                     #
# --------------------------------------------------------------------------- #

def _etSnouty(clockWidefield):
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    manager = _RecordingDetectorsManager()
    ctrl._master = _Master(manager)
    ctrl._detectorFastHandle = None
    ctrl.detectorFast = 'FAST'
    ctrl.ClockWidefield = clockWidefield
    ctrl._EtSnoutyController__logger = _Logger()
    return ctrl, manager


def test_self_clocked_mode_leases_event_direct():
    """It drives its own reads; polling would steal the frames it waits on."""
    ctrl, manager = _etSnouty(clockWidefield=True)

    EtSnoutyController._acquireDetectorFastLease(ctrl)

    assert manager.acquired == [(('FAST',), LeasePurpose.EVENT_DIRECT, 'handle-1')]


def test_signal_driven_mode_leases_event_stream():
    """It consumes sigUpdateImage, which only fires for frame-stream members —
    EVENT_DIRECT here would stall the mode with live view off."""
    ctrl, manager = _etSnouty(clockWidefield=False)

    EtSnoutyController._acquireDetectorFastLease(ctrl)

    assert manager.acquired == [(('FAST',), LeasePurpose.EVENT_STREAM, 'handle-1')]


def test_lease_is_taken_after_the_clock_mode_is_decided():
    """Source guard: the purpose depends on ClockWidefield, so acquiring
    before that branch would always pick the wrong one."""
    import inspect

    source = inspect.getsource(EtSnoutyController.initiate)
    clockDecision = source.index('self.ClockWidefield = True')
    leaseCall = source.index('self._acquireDetectorFastLease()')
    assert leaseCall > clockDecision


def test_etsnouty_release_is_idempotent():
    ctrl, manager = _etSnouty(clockWidefield=True)
    EtSnoutyController._acquireDetectorFastLease(ctrl)

    EtSnoutyController._releaseDetectorFastLease(ctrl)
    EtSnoutyController._releaseDetectorFastLease(ctrl)

    assert manager.released == ['handle-1']
    assert ctrl._detectorFastHandle is None


def test_etsnouty_does_not_stack_leases_across_starts():
    ctrl, manager = _etSnouty(clockWidefield=True)

    EtSnoutyController._acquireDetectorFastLease(ctrl)
    EtSnoutyController._acquireDetectorFastLease(ctrl)

    assert len(manager.acquired) == 1


# --------------------------------------------------------------------------- #
# etSTED/EtMonalisa: paused runs must still release                            #
# --------------------------------------------------------------------------- #

class _State:
    def __init__(self, imageSignalConnected):
        self.imageSignalConnected = imageSignalConnected
        self.scanEndSignalConnected = False
        self.scanInitiationMode = None


class _CommChannel:
    class _Signal:
        def connect(self, slot):
            pass

        def disconnect(self, slot):
            pass

        def emit(self, *args):
            pass

    def __init__(self):
        self.sigUpdateImage = self._Signal()
        self.sigScanEnded = self._Signal()
        self.sigRecordingEnded = self._Signal()
        self.sigToggleBlockScanWidget = self._Signal()


def _eventController(imageSignalConnected):
    ctrl = EventTriggeredControllerBase.__new__(EventTriggeredControllerBase)
    manager = _RecordingDetectorsManager()
    ctrl._master = _Master(manager)
    ctrl._commChannel = _CommChannel()
    ctrl._state = _State(imageSignalConnected)
    ctrl._logger = _Logger()
    ctrl._detectorFastHandle = manager.acquire(['FAST'],
                                               LeasePurpose.EVENT_STREAM)
    manager.acquired.clear()
    return ctrl, manager


def test_stop_releases_the_lease_of_a_paused_run():
    """pauseFastModality left imageSignalConnected False while keeping the
    lease; a guarded release would leak it for the rest of the session."""
    ctrl, manager = _eventController(imageSignalConnected=False)

    EventTriggeredControllerBase._disconnectRunSignals(ctrl)

    assert manager.released == ['handle-1']
    assert ctrl._detectorFastHandle is None


def test_stop_releases_the_lease_of_a_running_modality():
    ctrl, manager = _eventController(imageSignalConnected=True)

    EventTriggeredControllerBase._disconnectRunSignals(ctrl)

    assert manager.released == ['handle-1']
    assert ctrl._state.imageSignalConnected is False


def test_stop_without_a_lease_is_a_no_op():
    ctrl, manager = _eventController(imageSignalConnected=False)
    ctrl._detectorFastHandle = None

    EventTriggeredControllerBase._disconnectRunSignals(ctrl)

    assert manager.released == []


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

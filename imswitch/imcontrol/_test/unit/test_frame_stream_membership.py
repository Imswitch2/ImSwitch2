"""Frame-stream membership drives frame delivery (Phase 4).

Arming a detector and delivering its frames are different questions. The poll
loop used to read every ``forAcquisition`` detector whenever live view was on,
and nothing at all when it was off — so an event-detection loop (etSTED /
EtMonalisa) with live view off had its ``detectorFast`` armed but never read,
and stalled waiting for frames that could not arrive.

Delivery is now driven by ``frameStreamMembership = LIVE_VIEW | EVENT_STREAM``.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import sys

import pytest

from imswitch.imcontrol.model.managers.DetectorsManager import (
    DetectorsManager, LVWorker,
)
from imswitch.imcontrol.model.managers._acquisition_leases import (
    AcquisitionLeaseTable, LeasePurpose,
)


class _StubDetector:
    def __init__(self, name, forAcquisition=True, isScanDriven=False):
        self.name = name
        self.forAcquisition = forAcquisition
        self.isScanDriven = isScanDriven
        self.polls = []
        self._acquisitionLeased = False
        self._hardwareFaulted = False

    def startAcquisition(self):
        pass

    def stopAcquisition(self):
        pass

    def updateLatestFrame(self, init):
        self.polls.append(init)


class _StubThread:
    def __init__(self):
        self.startCalls = 0
        self.quitCalls = 0

    def start(self):
        self.startCalls += 1

    def quit(self):
        self.quitCalls += 1

    def wait(self):
        pass

    def isRunning(self):
        return self.startCalls > self.quitCalls


class _StubSignal:
    def emit(self, *args):
        pass


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(
        sys.modules['imswitch.imcontrol.model.managers.DetectorsManager'],
        'sleep', lambda _seconds: None,
    )

    mgr = DetectorsManager.__new__(DetectorsManager)
    detectors = {
        'CAM': _StubDetector('CAM'),
        'FAST': _StubDetector('FAST'),
        'APD': _StubDetector('APD', isScanDriven=True),
    }
    mgr._subManagers = detectors
    mgr._currentDetectorName = 'CAM'
    mgr._thread = _StubThread()
    mgr.sigAcquisitionStarted = _StubSignal()
    mgr.sigAcquisitionStopped = _StubSignal()
    mgr._leaseTable = AcquisitionLeaseTable(
        startDetector=lambda name: detectors[name].startAcquisition(),
        stopDetector=lambda name: detectors[name].stopAcquisition(),
        onStateChanged=mgr._DetectorsManager__onLeaseStateChanged,
    )
    return mgr


# --------------------------------------------------------------------------- #
# Membership                                                                   #
# --------------------------------------------------------------------------- #

def test_membership_is_live_view_union_event_stream(manager):
    manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    manager.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    manager.acquire(['APD'], LeasePurpose.SCAN)

    assert manager.frameStreamMembership() == {'CAM', 'FAST'}


def test_armed_but_non_streaming_detectors_are_not_polled(manager):
    """A SCAN or RECORDING lease arms hardware; it does not put the detector
    on the poll loop."""
    manager.acquire(['APD'], LeasePurpose.SCAN)
    manager.acquire(['CAM'], LeasePurpose.RECORDING)

    assert manager.frameStreamMembership() == set()


def test_event_direct_consumers_are_not_polled(manager):
    """EtSnouty reads frames itself via wait_and_get_NewFrame."""
    manager.acquire(['FAST'], LeasePurpose.EVENT_DIRECT)

    assert manager.frameStreamMembership() == set()


# --------------------------------------------------------------------------- #
# Poll-thread lifecycle                                                        #
# --------------------------------------------------------------------------- #

def test_event_stream_lease_keeps_polling_with_live_view_off(manager):
    """R6-1, the stall this phase fixes: turning live view off must not stop
    the detection loop's frames."""
    liveHandle = manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    manager.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    assert manager._thread.startCalls == 1

    manager.release(liveHandle)  # live view off

    assert manager._thread.quitCalls == 0          # poll thread still running
    assert manager.frameStreamMembership() == {'FAST'}


def test_poll_thread_starts_for_an_event_stream_lease_alone(manager):
    manager.acquire(['FAST'], LeasePurpose.EVENT_STREAM)

    assert manager._thread.startCalls == 1


def test_poll_thread_stops_only_when_the_last_streamer_goes(manager):
    live = manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    event = manager.acquire(['FAST'], LeasePurpose.EVENT_STREAM)

    manager.release(live)
    assert manager._thread.quitCalls == 0
    manager.release(event)
    assert manager._thread.quitCalls == 1


def test_scan_lease_does_not_start_the_poll_thread(manager):
    manager.acquire(['APD'], LeasePurpose.SCAN)

    assert manager._thread.startCalls == 0


def test_set_update_period_does_not_resurrect_an_idle_poll_thread(manager):
    """R7-s7: this used to quit+start unconditionally, starting a poll thread
    with nothing to poll."""
    manager._lvWorker = LVWorker(manager, 100)

    manager.setUpdatePeriod(50)

    assert manager._thread.startCalls == 0
    assert manager._thread.quitCalls == 0


def test_set_update_period_restarts_a_running_poll_thread(manager):
    manager._lvWorker = LVWorker(manager, 100)
    manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)

    manager.setUpdatePeriod(50)

    assert manager._thread.startCalls == 2
    assert manager._thread.quitCalls == 1


# --------------------------------------------------------------------------- #
# What the worker actually polls                                               #
# --------------------------------------------------------------------------- #

def test_worker_polls_exactly_the_membership(manager):
    manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    manager.acquire(['APD'], LeasePurpose.SCAN)
    worker = LVWorker(manager, 100)

    worker._pollFrameStream(init=True)

    assert manager['CAM'].polls == [True]
    assert manager['APD'].polls == []   # armed, but not a streamer
    assert manager['FAST'].polls == []


def test_worker_picks_up_a_lease_taken_mid_flight(manager):
    """Membership is re-read every tick, so an event loop starting while live
    view is already running does not need the thread restarted."""
    manager.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    worker = LVWorker(manager, 100)
    worker._pollFrameStream(init=True)

    manager.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    worker._pollFrameStream(init=True)

    assert manager['CAM'].polls == [True, True]
    assert manager['FAST'].polls == [True]


def test_one_failing_detector_does_not_kill_the_poll_loop(manager):
    def explode(init):
        raise RuntimeError('detector went away')

    manager.acquire(['CAM', 'FAST'], LeasePurpose.LIVE_VIEW)
    manager['CAM'].updateLatestFrame = explode
    worker = LVWorker(manager, 100)

    worker._pollFrameStream(init=True)

    assert manager['FAST'].polls == [True]


def test_settle_delay_lives_on_the_worker_thread_not_the_caller():
    """The 300 ms settle used to block whoever started live view — i.e. the
    UI thread. It must stay inside the worker's own run()."""
    import inspect

    managerSource = inspect.getsource(DetectorsManager._DetectorsManager__acquireImpl)
    assert 'sleep(' not in managerSource

    assert 'sleep(self._INITIAL_SETTLE_S)' in inspect.getsource(LVWorker.run)


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

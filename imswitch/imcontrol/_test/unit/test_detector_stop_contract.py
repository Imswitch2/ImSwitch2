"""Detector stop contract (Phase 1).

``stopAcquisition()`` used to swallow teardown errors in APDManager,
PMTManager and SwabianTimeTaggerManager, so the DetectorsManager could never
tell a clean stop from a broken one — a point detector whose scan thread never
died would silently rejoin the next scan. The contract: the
DetectorsManager-facing ``stopAcquisition()`` RAISES on teardown failure
(logging first is fine). Internal worker-completion paths
(``acqDoneSignal`` -> ``stopAcquisitionLocal``) keep swallowing: no lease
operation is in flight there to report a fault to.

These tests drive the real stop methods on instances built with
``__new__`` (no hardware, no Qt), with only the attributes the stop path
touches.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.managers.detectors.AVManager import AVManager
from imswitch.imcontrol.model.managers.detectors.PiCamManager import PiCamManager
from imswitch.imcontrol.model.managers.detectors.TISManager import TISManager


class _ExplodingThread:
    """A scan thread whose teardown fails, like a QThread that won't join."""

    def __init__(self):
        self.waitTimeouts = []

    def quit(self):
        raise RuntimeError('thread refused to quit')

    def wait(self, timeout):
        self.waitTimeouts.append(timeout)
        return False

    def isRunning(self):
        return True


class _CleanThread:
    def __init__(self):
        self.quitCalls = 0
        self.running = True
        self.waitTimeouts = []

    def quit(self):
        self.quitCalls += 1

    def wait(self, timeout):
        self.waitTimeouts.append(timeout)
        self.running = False
        return True

    def isRunning(self):
        return self.running


class _StuckThread(_CleanThread):
    def wait(self, timeout):
        self.waitTimeouts.append(timeout)
        return False


class _FrameworkThread:
    """Matches framework.Thread, whose wait() has no timeout parameter."""

    def __init__(self):
        self.quitCalls = 0
        self.waitCalls = 0
        self.running = True

    def quit(self):
        self.quitCalls += 1
        self.running = False

    def wait(self):
        self.waitCalls += 1

    def isRunning(self):
        return self.running


class _Worker:
    def __init__(self, *, failClose=False):
        self.scanning = True
        self.closed = False
        self.failClose = failClose

    def close(self):
        if self.failClose:
            raise RuntimeError('input task close failed')
        self.closed = True

    def stop(self):
        pass


class _Logger:
    def __init__(self):
        self.messages = []

    def warning(self, msg, *args, **kwargs):
        self.messages.append(msg)

    def exception(self, msg, *args, **kwargs):
        self.messages.append(msg)

    def error(self, msg, *args, **kwargs):
        self.messages.append(msg)

    def debug(self, msg, *args, **kwargs):
        pass

    def info(self, msg, *args, **kwargs):
        pass


class _RetryableCamera:
    def __init__(self):
        self.stopCalls = 0

    def suspend_live(self):
        self.stopCalls += 1
        if self.stopCalls == 1:
            raise RuntimeError('SDK stop failed')

    def close(self):
        pass


def _makeAPD(thread, *, worker=None):
    apd = APDManager.__new__(APDManager)
    apd._scanWorker = worker or _Worker()
    apd._scanThread = thread
    apd._APDManager__logger = _Logger()
    apd._APDManager__currSlice = (0, 0)
    apd._APDManager__newFrameReady = False
    apd._debug_mode = False
    apd._ttlmultiplying = False
    return apd


def _makePMT(thread, *, worker=None):
    pmt = PMTManager.__new__(PMTManager)
    pmt._scanWorker = worker or _Worker()
    pmt._scanThread = thread
    pmt._PMTManager__logger = _Logger()
    pmt._PMTManager__newFrameReady = False
    pmt._debug_mode = False
    pmt._ttlmultiplying = False
    return pmt


def _makeTimeTagger(failTeardown):
    tt = SwabianTimeTaggerManager.__new__(SwabianTimeTaggerManager)
    tt._logger = _Logger()
    tt.acquisition = True
    tt._newFrameReady = False

    def teardown():
        if failTeardown:
            raise RuntimeError('teardown failed')

    tt._teardownScanThread = teardown
    return tt


# --------------------------------------------------------------------------- #
# APD                                                                          #
# --------------------------------------------------------------------------- #

def test_apd_stop_raises_when_teardown_fails():
    apd = _makeAPD(_ExplodingThread())

    with pytest.raises(RuntimeError, match='thread refused to quit'):
        apd.stopAcquisition()

    assert apd._APDManager__logger.messages  # still logged
    apd._scanThread = None  # __del__ would re-raise at GC time


def test_apd_stop_succeeds_normally():
    thread = _CleanThread()
    apd = _makeAPD(thread)

    apd.stopAcquisition()

    assert thread.quitCalls == 1
    assert thread.waitTimeouts == [2000]
    assert apd._scanWorker is None
    assert apd._scanThread is None


def test_apd_stop_is_a_no_op_without_a_scan():
    apd = _makeAPD(None)
    apd._scanWorker = None

    apd.stopAcquisition()  # must not raise


def test_apd_internal_completion_path_still_swallows():
    """acqDoneSignal -> stopAcquisitionLocal has no lease operation to fault."""
    apd = _makeAPD(_ExplodingThread())

    apd.stopAcquisitionLocal()  # must not raise

    assert apd._APDManager__logger.messages
    apd._scanThread = None  # __del__ would re-raise at GC time


# --------------------------------------------------------------------------- #
# PMT                                                                          #
# --------------------------------------------------------------------------- #

def test_pmt_stop_raises_when_teardown_fails():
    pmt = _makePMT(_ExplodingThread())

    with pytest.raises(RuntimeError, match='thread refused to quit'):
        pmt.stopAcquisition()


def test_pmt_stop_succeeds_normally():
    thread = _CleanThread()
    pmt = _makePMT(thread)

    pmt.stopAcquisition()

    assert thread.quitCalls == 1
    assert thread.waitTimeouts == [2000]
    assert pmt._scanWorker is None
    assert pmt._scanThread is None


def test_pmt_internal_completion_path_still_swallows():
    pmt = _makePMT(_ExplodingThread())

    pmt.stopAcquisitionLocal()  # must not raise

    assert pmt._PMTManager__logger.messages


@pytest.mark.parametrize(
    ('managerClass', 'loggerAttribute'),
    [
        (AVManager, '_AVManager__logger'),
        (PiCamManager, '_PiCamManager__logger'),
        (TISManager, '_TISManager__logger'),
    ],
)
def test_camera_stop_failure_retains_running_state_for_retry(
        managerClass, loggerAttribute):
    manager = managerClass.__new__(managerClass)
    manager._running = True
    manager._camera = _RetryableCamera()
    setattr(manager, loggerAttribute, _Logger())

    with pytest.raises(RuntimeError, match='SDK stop failed'):
        manager.stopAcquisition()
    assert manager._running is True

    manager.stopAcquisition()
    assert manager._camera.stopCalls == 2
    assert manager._running is False


@pytest.mark.parametrize('factory', [_makeAPD, _makePMT])
def test_point_detector_input_close_failure_uses_bounded_join_and_retains_thread(
        factory):
    thread = _StuckThread()
    worker = _Worker(failClose=True)
    worker.scanGeneration = 12
    manager = factory(thread, worker=worker)
    manager._preparedScanGeneration = 12
    manager._activeScanGeneration = 12

    with pytest.raises(RuntimeError, match='input task close failed'):
        manager.stopAcquisition()

    # Closing the NI task failed, so waiting forever would deadlock shutdown.
    # The bounded attempt is recorded and the live objects stay strongly held
    # for an explicit retry rather than being destroyed while still running.
    assert thread.waitTimeouts == [2000]
    assert manager._scanWorker is worker
    assert manager._scanThread is thread
    assert manager._preparedScanGeneration == 12
    assert manager._activeScanGeneration == 12

    # Avoid a destructor retry against the deliberately stuck test double.
    manager._scanWorker = None
    manager._scanThread = None


@pytest.mark.parametrize('factory', [_makeAPD, _makePMT])
def test_point_detector_abort_acknowledges_after_bounded_cleanup_failure(factory):
    thread = _StuckThread()
    worker = _Worker(failClose=True)
    worker.scanGeneration = 13
    manager = factory(thread, worker=worker)
    manager._activeScanGeneration = 13
    acknowledgements = []

    manager.finishScan('abort', lambda: acknowledgements.append(True))

    assert acknowledgements == [True]
    assert manager._scanWorker is worker
    assert manager._scanThread is thread

    manager._scanWorker = None
    manager._scanThread = None


@pytest.mark.parametrize('factory', [_makeAPD, _makePMT])
def test_point_detector_teardown_supports_framework_thread_wait(factory):
    thread = _FrameworkThread()
    manager = factory(thread)

    manager.stopAcquisition()

    assert thread.quitCalls == 1
    assert thread.waitCalls == 1
    assert manager._scanWorker is None
    assert manager._scanThread is None


# --------------------------------------------------------------------------- #
# Swabian TimeTagger                                                           #
# --------------------------------------------------------------------------- #

def test_timetagger_stop_raises_when_teardown_fails():
    tt = _makeTimeTagger(failTeardown=True)

    with pytest.raises(RuntimeError, match='teardown failed'):
        tt.stopAcquisition()

    assert tt.acquisition is False
    assert tt._newFrameReady is True  # flag still set on the failure path
    assert tt._logger.messages


def test_timetagger_stop_succeeds_normally():
    tt = _makeTimeTagger(failTeardown=False)

    tt.stopAcquisition()

    assert tt.acquisition is False
    assert tt._newFrameReady is True


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

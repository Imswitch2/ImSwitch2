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


class _ExplodingThread:
    """A scan thread whose teardown fails, like a QThread that won't join."""

    def quit(self):
        raise RuntimeError('thread refused to quit')

    def wait(self):
        pass

    def isRunning(self):
        return True


class _CleanThread:
    def __init__(self):
        self.quitCalls = 0

    def quit(self):
        self.quitCalls += 1

    def wait(self):
        pass

    def isRunning(self):
        return True


class _Worker:
    def __init__(self):
        self.scanning = True
        self.closed = False

    def close(self):
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


def _makeAPD(thread):
    apd = APDManager.__new__(APDManager)
    apd._scanWorker = _Worker()
    apd._scanThread = thread
    apd._APDManager__logger = _Logger()
    apd._APDManager__currSlice = (0, 0)
    apd._APDManager__newFrameReady = False
    apd._debug_mode = False
    apd._ttlmultiplying = False
    return apd


def _makePMT(thread):
    pmt = PMTManager.__new__(PMTManager)
    pmt._scanWorker = _Worker()
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
    assert pmt._scanWorker is None
    assert pmt._scanThread is None


def test_pmt_internal_completion_path_still_swallows():
    pmt = _makePMT(_ExplodingThread())

    pmt.stopAcquisitionLocal()  # must not raise

    assert pmt._PMTManager__logger.messages


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

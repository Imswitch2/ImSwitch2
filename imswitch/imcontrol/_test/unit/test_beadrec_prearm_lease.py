"""BeadRec pre-arm ownership (Phase 4, R6-5).

BeadRec armed its camera at ``sigScanStarted``, which is too late: a
trigger-driven camera armed at scan start has already missed the first TTL
pulses, so the reconstruction comes out short or shifted by a frame. It now
declares its lease at ``sigScanStarting``, before the scan is built and before
any TTL output.

It also resolved the reconstruction source per chunk via ``execOnCurrent``, so
changing the current detector in the view mid-scan silently switched which
camera the reconstruction was built from. The name is now pinned per scan.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcontrol.controller.controllers.BeadRecController import (
    BeadRecController,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


class _Detector:
    def __init__(self, name):
        self.name = name
        self.chunks = []

    def readChunk(self, consumerKey):
        self.chunks.append(consumerKey)
        return [f'{self.name}-frame']


class _DetectorsManager:
    def __init__(self, currentName='CAM1'):
        self._detectors = {'CAM1': _Detector('CAM1'), 'CAM2': _Detector('CAM2')}
        self._currentName = currentName
        self.acquired = []
        self.released = []
        self._seq = 0

    def getCurrentDetectorName(self):
        return self._currentName

    def setCurrentDetectorName(self, name):
        self._currentName = name

    def execOn(self, name, func):
        return func(self._detectors[name])

    def execOnCurrent(self, func):
        return func(self._detectors[self._currentName])

    def acquire(self, detectorNames, purpose):
        self._seq += 1
        handle = f'lease-{self._seq}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)

    def __getitem__(self, name):
        return self._detectors[name]


class _Master:
    def __init__(self, detectorsManager):
        self.detectorsManager = detectorsManager


class _RunButton:
    def __init__(self, checked=True):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _Widget:
    def __init__(self, running=True):
        self.runButton = _RunButton(running)


class _Logger:
    def error(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _controller(running=True, currentName='CAM1'):
    """A bare controller carrying only the pre-arm state, running the real
    methods (constructing the full widget stack is not needed for ownership)."""
    ctrl = BeadRecController.__new__(BeadRecController)
    manager = _DetectorsManager(currentName=currentName)
    ctrl._master = _Master(manager)
    ctrl._widget = _Widget(running)
    ctrl._logger = _Logger()
    ctrl._scanDetectorHandle = None
    ctrl._scanDetectorName = None
    ctrl._CHUNK_CONSUMER = 'BeadRec'
    return ctrl, manager


# --------------------------------------------------------------------------- #
# Pre-arm                                                                      #
# --------------------------------------------------------------------------- #

def test_pre_arm_leases_the_current_detector():
    ctrl, manager = _controller()

    BeadRecController.onScanStarting(ctrl)

    assert manager.acquired == [(('CAM1',), LeasePurpose.WORKFLOW, 'lease-1')]
    assert ctrl._scanDetectorName == 'CAM1'


def test_no_lease_when_beadrec_is_not_running():
    ctrl, manager = _controller(running=False)

    BeadRecController.onScanStarting(ctrl)

    assert manager.acquired == []
    assert ctrl._scanDetectorName is None


def test_pre_arm_releases_a_previous_scan_lease_first():
    """Back-to-back scans must not stack leases."""
    ctrl, manager = _controller()

    BeadRecController.onScanStarting(ctrl)
    BeadRecController.onScanStarting(ctrl)

    assert manager.released == ['lease-1']
    assert len(manager.acquired) == 2


def test_release_is_idempotent():
    ctrl, manager = _controller()
    BeadRecController.onScanStarting(ctrl)

    BeadRecController._releaseScanDetectorLease(ctrl)
    BeadRecController._releaseScanDetectorLease(ctrl)

    assert manager.released == ['lease-1']
    assert ctrl._scanDetectorHandle is None


def test_a_failed_lease_does_not_pin_a_detector():
    """If arming fails, reconstruction must fall back rather than read from a
    detector it never armed."""
    ctrl, manager = _controller()

    def failingAcquire(detectorNames, purpose):
        raise RuntimeError('detector busy')

    manager.acquire = failingAcquire
    BeadRecController.onScanStarting(ctrl)

    assert ctrl._scanDetectorName is None


# --------------------------------------------------------------------------- #
# Pinned reconstruction source                                                 #
# --------------------------------------------------------------------------- #

def test_reconstruction_source_is_pinned_for_the_whole_scan():
    """Switching the view's current detector mid-scan must not switch which
    camera the reconstruction is built from."""
    ctrl, manager = _controller(currentName='CAM1')
    BeadRecController.onScanStarting(ctrl)

    manager.setCurrentDetectorName('CAM2')

    assert BeadRecController._reconstructionDetectorName(ctrl) == 'CAM1'


def test_chunks_come_from_the_pinned_detector():
    ctrl, manager = _controller(currentName='CAM1')
    BeadRecController.onScanStarting(ctrl)
    manager.setCurrentDetectorName('CAM2')

    ctrl._toDisplayedFrames = lambda frames: frames
    frames = BeadRecController._getCurrentDetectorChunk(ctrl)

    assert frames == ['CAM1-frame']
    assert manager['CAM2'].chunks == []


def test_falls_back_to_the_current_detector_outside_a_scan():
    ctrl, manager = _controller(currentName='CAM2')

    assert BeadRecController._reconstructionDetectorName(ctrl) == 'CAM2'


def test_pin_is_cleared_by_releasing_and_re_pinned_next_scan():
    ctrl, manager = _controller(currentName='CAM1')
    BeadRecController.onScanStarting(ctrl)
    assert ctrl._scanDetectorName == 'CAM1'

    manager.setCurrentDetectorName('CAM2')
    BeadRecController.onScanStarting(ctrl)

    assert ctrl._scanDetectorName == 'CAM2'


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

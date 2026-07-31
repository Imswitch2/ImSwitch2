"""CamFacade acquisition ownership (Phase 2).

CamFacade used to call ``startAcquisition``/``stopAcquisition`` straight on
the sub-manager, so a workflow could disarm a detector another consumer was
using (and vice versa). It now holds a WORKFLOW lease when it is given the
DetectorsManager, and keeps the legacy raw calls when it is not (the facade
is also constructed directly around a bare detector in tests and scripts).

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.workflows.facade import CamFacade


class _BareDetector:
    """Deliberately has no ``name`` — construction must not touch it."""

    def __init__(self):
        self.startCalls = 0
        self.stopCalls = 0

    def startAcquisition(self):
        self.startCalls += 1

    def stopAcquisition(self):
        self.stopCalls += 1

    def releaseChunkConsumer(self, key):
        pass


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


def _leasedFacade():
    detector = _BareDetector()
    manager = _RecordingDetectorsManager()
    cam = CamFacade(detector, detectorsManager=manager, detectorName='CAM')
    return cam, detector, manager


def test_acquisition_lifecycle_takes_and_releases_a_workflow_lease():
    cam, detector, manager = _leasedFacade()

    cam.start_acquisition()
    assert manager.acquired == [(('CAM',), LeasePurpose.WORKFLOW, 'handle-1')]
    assert detector.startCalls == 0  # no bypass

    cam.stop_acquisition()
    assert manager.released == ['handle-1']
    assert detector.stopCalls == 0


def test_live_lifecycle_takes_and_releases_a_workflow_lease():
    cam, _, manager = _leasedFacade()

    cam.start_live()
    cam.stop_live()

    assert len(manager.acquired) == 1
    assert manager.released == ['handle-1']


def test_repeated_arming_does_not_leak_leases():
    """Workflows re-arm per plane; a double start must not strand a lease."""
    cam, _, manager = _leasedFacade()

    cam.start_acquisition()
    cam.start_acquisition()
    cam.stop_acquisition()

    assert len(manager.acquired) == 1
    assert manager.released == ['handle-1']


def test_stop_without_start_is_a_no_op():
    cam, detector, manager = _leasedFacade()

    cam.stop_acquisition()

    assert manager.released == []
    assert detector.stopCalls == 0


def test_arm_disarm_cycles_are_balanced():
    cam, _, manager = _leasedFacade()

    for _ in range(3):
        cam.start_acquisition()
        cam.stop_acquisition()

    assert len(manager.acquired) == 3
    assert len(manager.released) == 3


def test_without_a_manager_it_keeps_the_legacy_raw_calls():
    detector = _BareDetector()
    cam = CamFacade(detector)

    cam.start_acquisition()
    cam.stop_acquisition()

    assert detector.startCalls == 1
    assert detector.stopCalls == 1


def test_construction_does_not_touch_the_detector():
    """A bare detector may not be able to serve attribute access (fakes and
    partially-initialized managers raise), so the facade must not probe it."""

    class _Exploding:
        def __getattr__(self, item):
            raise RuntimeError(f'must not be touched: {item}')

    CamFacade(_Exploding())  # must not raise
    CamFacade(_Exploding(), detectorsManager=object(), detectorName='CAM')


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

"""Focus-lock / autofocus acquisition ownership (Phase 2).

Both controllers used to reach *past* the DetectorsManager straight to the
sub-manager (``detectorsManager[camera].startAcquisition()``), so the lease
table never knew their camera was armed — an unrelated global stop could
disarm the focus camera underneath them — and neither ever released it.

They now hold a FOCUS lease, which is refcounted like any other and is
excluded from the user-visible acquisition signals, and release it on
``closeEvent`` (acceptance #10).

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import inspect

import pytest

from imswitch.imcontrol.controller.controllers.AutofocusController import (
    AutofocusController,
)
from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


class _RecordingDetectorsManager:
    """Captures acquire/release calls without any hardware."""

    def __init__(self):
        self.acquired = []
        self.released = []
        self._handleSeq = 0

    def acquire(self, detectorNames, purpose):
        self._handleSeq += 1
        handle = f'handle-{self._handleSeq}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


class _Master:
    def __init__(self, detectorsManager):
        self.detectorsManager = detectorsManager


@pytest.mark.parametrize('controllerClass', [AutofocusController, FocusLockController])
def test_focus_controller_takes_a_lease_instead_of_bypassing_the_manager(
        controllerClass):
    """Source-level contract: constructing the full controller needs the whole
    widget/factory stack, so guard the ownership rule where the bug lived —
    the sub-manager bypass must not come back."""
    source = inspect.getsource(controllerClass.__init__)

    assert 'detectorsManager.acquire(' in source
    assert 'LeasePurpose.FOCUS' in source
    # The bypass this replaced: detectorsManager[self.camera].startAcquisition()
    assert 'startAcquisition()' not in source


@pytest.mark.parametrize('controllerClass', [AutofocusController, FocusLockController])
def test_focus_controller_releases_its_lease_on_close(controllerClass):
    manager = _RecordingDetectorsManager()
    ctrl = controllerClass.__new__(controllerClass)
    ctrl._master = _Master(manager)
    ctrl._focusAcqHandle = manager.acquire(['FocusCam'], LeasePurpose.FOCUS)

    controllerClass.closeEvent(ctrl)

    assert manager.released == ['handle-1']
    assert ctrl._focusAcqHandle is None


@pytest.mark.parametrize('controllerClass', [AutofocusController, FocusLockController])
def test_close_without_a_lease_is_a_no_op(controllerClass):
    """Setups without focusLock/autofocus configured return early from
    __init__ before acquiring, so closeEvent must tolerate no handle."""
    manager = _RecordingDetectorsManager()
    ctrl = controllerClass.__new__(controllerClass)
    ctrl._master = _Master(manager)
    ctrl._focusAcqHandle = None

    controllerClass.closeEvent(ctrl)

    assert manager.released == []


@pytest.mark.parametrize('controllerClass', [AutofocusController, FocusLockController])
def test_close_is_idempotent(controllerClass):
    manager = _RecordingDetectorsManager()
    ctrl = controllerClass.__new__(controllerClass)
    ctrl._master = _Master(manager)
    ctrl._focusAcqHandle = manager.acquire(['FocusCam'], LeasePurpose.FOCUS)

    controllerClass.closeEvent(ctrl)
    controllerClass.closeEvent(ctrl)

    assert manager.released == ['handle-1']  # not released twice


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

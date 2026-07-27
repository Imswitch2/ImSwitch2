"""Per-detector acquisition selection (Phase 5 — the perf win).

Selection is the *seed* for lease composition, never an authority over
hardware. Three rules follow from that, and each has cost real acquisition
time when a system got it wrong:

* a deselected scan-driven detector is left out of the scan, so a 1000x1000
  z-stack no longer pays for a PMT nobody is reading;
* a detector an explicit consumer is holding — recording, workflow, event
  modality — runs regardless of the checkbox, so deselecting cannot silently
  corrupt an acquisition already under way;
* a change that cannot apply now is queued, never rejected. An auto-repeat
  scan runs iterations back to back, so rejecting would leave the user no
  usable window at all.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)


class _Detector:
    def __init__(self, name, isScanDriven=False, forAcquisition=True):
        self.name = name
        self.isScanDriven = isScanDriven
        self.forAcquisition = forAcquisition


class _DetectorsManager:
    """Selection surface of the real manager, without Qt or hardware."""

    def __init__(self, detectors, leased=None, faulted=()):
        self._detectors = {d.name: d for d in detectors}
        self._leased = leased or {}
        self._faulted = set(faulted)
        self._selected = {d.name for d in detectors if d.forAcquisition}
        self._queued = {}
        self.flushCalls = 0

    def getAllDeviceNames(self, condition=None):
        return [n for n, d in self._detectors.items()
                if condition is None or condition(d)]

    def isDetectorFaulted(self, name):
        return name in self._faulted

    def leasedDetectorNames(self, purposes=None):
        purposeSet = None if purposes is None else set(purposes)
        return {name for name, purpose in self._leased.items()
                if purposeSet is None or purpose in purposeSet}

    def getSelectedDetectors(self):
        return set(self._selected)

    def setDetectorSelected(self, name, selected):
        self._selected.add(name) if selected else self._selected.discard(name)

    def queueSelection(self, name, selected):
        self._queued[name] = selected

    def flushQueuedSelectionChanges(self):
        self.flushCalls += 1
        queued, self._queued = self._queued, {}
        for name, selected in queued.items():
            self.setDetectorSelected(name, selected)
        return queued


class _Nidaq:
    def runScan(self, signalDict, scanInfoDict):
        pass


def _coordinator(leased=None, faulted=()):
    manager = _DetectorsManager(
        [_Detector('APD', isScanDriven=True),
         _Detector('PMT', isScanDriven=True),
         _Detector('Camera')],
        leased=leased, faulted=faulted,
    )
    return ScanExecutionCoordinator(manager, _Nidaq()), manager


# --------------------------------------------------------------------------- #
# Selection narrows the scan                                                   #
# --------------------------------------------------------------------------- #

def test_everything_participates_by_default():
    coordinator, _ = _coordinator()

    assert coordinator.composeParticipants() == ['APD', 'PMT']


def test_a_deselected_detector_is_left_out_of_the_scan():
    """The perf win: this is what stops a scan paying for an unread PMT."""
    coordinator, manager = _coordinator()

    manager.setDetectorSelected('PMT', False)

    assert coordinator.composeParticipants() == ['APD']


def test_deselecting_a_camera_does_not_affect_the_scan_snapshot():
    """Only scan-driven detectors are ever in the participant snapshot."""
    coordinator, manager = _coordinator()

    manager.setDetectorSelected('Camera', False)

    assert coordinator.composeParticipants() == ['APD', 'PMT']


def test_deselecting_everything_yields_an_empty_snapshot():
    coordinator, manager = _coordinator()

    manager.setDetectorSelected('APD', False)
    manager.setDetectorSelected('PMT', False)

    assert coordinator.composeParticipants() == []


# --------------------------------------------------------------------------- #
# Explicit consumers override the checkbox                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('purpose', [
    LeasePurpose.RECORDING, LeasePurpose.SNAP, LeasePurpose.WORKFLOW,
    LeasePurpose.EVENT_STREAM, LeasePurpose.EVENT_DIRECT,
    LeasePurpose.GENERIC,
])
def test_a_held_detector_participates_even_when_deselected(purpose):
    """Deselecting is a preference; an active consumer is a commitment.
    Dropping its detector mid-run would corrupt what it is producing."""
    coordinator, manager = _coordinator(leased={'PMT': purpose})

    manager.setDetectorSelected('PMT', False)

    assert 'PMT' in coordinator.composeParticipants()


def test_a_scan_lease_does_not_override_selection():
    """Only the SCAN lease itself is composed from selection, so letting it
    override would make a deselection impossible to ever apply."""
    coordinator, manager = _coordinator(leased={'PMT': LeasePurpose.SCAN})

    manager.setDetectorSelected('PMT', False)

    assert coordinator.composeParticipants() == ['APD']


def test_a_focus_lease_does_not_override_selection():
    coordinator, manager = _coordinator(leased={'PMT': LeasePurpose.FOCUS})

    manager.setDetectorSelected('PMT', False)

    assert coordinator.composeParticipants() == ['APD']


# --------------------------------------------------------------------------- #
# Faults outrank everything                                                    #
# --------------------------------------------------------------------------- #

def test_a_faulted_detector_never_participates_however_it_is_requested():
    """Its physical state is unknown; selection and leases cannot readmit it."""
    coordinator, manager = _coordinator(
        leased={'PMT': LeasePurpose.RECORDING}, faulted=('PMT',)
    )

    assert coordinator.composeParticipants() == ['APD']


# --------------------------------------------------------------------------- #
# Deferred changes                                                             #
# --------------------------------------------------------------------------- #

def test_queued_changes_are_applied_when_participants_are_composed():
    """The one point where a new selection can take effect without disturbing
    an iteration already in flight."""
    coordinator, manager = _coordinator()
    manager.queueSelection('PMT', False)

    assert coordinator.composeParticipants() == ['APD']
    assert manager.flushCalls == 1


def test_composition_flushes_before_reading_the_selection():
    coordinator, manager = _coordinator()
    manager.setDetectorSelected('APD', False)
    manager.queueSelection('APD', True)

    assert 'APD' in coordinator.composeParticipants()


# --------------------------------------------------------------------------- #
# Back-compatibility                                                           #
# --------------------------------------------------------------------------- #

def test_a_manager_without_selection_support_behaves_as_all_selected():
    """Embedders and older fixtures must not lose their detectors."""

    class _Bare:
        def getAllDeviceNames(self, condition=None):
            detectors = [_Detector('APD', isScanDriven=True)]
            return [d.name for d in detectors
                    if condition is None or condition(d)]

        def isDetectorFaulted(self, name):
            return False

    coordinator = ScanExecutionCoordinator(_Bare(), _Nidaq())

    assert coordinator.composeParticipants() == ['APD']


# --------------------------------------------------------------------------- #
# The "Acquire with" controls                                                  #
# --------------------------------------------------------------------------- #

class _SelectionWidget:
    """Mirrors ViewWidget's selection surface."""

    def __init__(self):
        self.options = []
        self.checked = {}
        self.pending = {}

    def setDetectorSelectionOptions(self, names):
        self.options = list(names)
        self.checked = {name: True for name in names}

    def setDetectorSelected(self, name, selected):
        self.checked[name] = selected

    def setDetectorSelectionPending(self, name, pending):
        self.pending[name] = pending


class _SelectionManager:
    def __init__(self, names, appliesNow=True):
        self._names = list(names)
        self._selected = set(names)
        self._appliesNow = appliesNow
        self.calls = []

    def getAllDeviceNames(self, condition=None):
        return list(self._names)

    def getSelectedDetectors(self):
        return set(self._selected)

    def isDetectorSelected(self, name):
        return name in self._selected

    def setDetectorSelected(self, name, selected):
        self.calls.append((name, selected))
        if not self._appliesNow:
            return False
        self._selected.add(name) if selected else self._selected.discard(name)
        return True


def _viewController(names=('Camera', 'APD'), appliesNow=True):
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl = ViewController.__new__(ViewController)
    manager = _SelectionManager(names, appliesNow)
    ctrl._master = type('M', (), {'detectorsManager': manager})()
    ctrl._widget = _SelectionWidget()
    ctrl._logger = type('L', (), {'error': lambda *a, **k: None})()
    return ctrl, manager


def test_controls_are_populated_from_the_current_selection():
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl, manager = _viewController()
    manager._selected.discard('APD')

    ViewController._populateDetectorSelection(ctrl)

    assert ctrl._widget.options == ['Camera', 'APD']
    assert ctrl._widget.checked == {'Camera': True, 'APD': False}


def test_toggling_a_box_reaches_the_model():
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl, manager = _viewController()

    ViewController._onDetectorSelectionToggled(ctrl, 'APD', False)

    assert manager.calls == [('APD', False)]
    assert ctrl._widget.pending['APD'] is False


def test_a_change_a_scan_defers_is_shown_as_pending():
    """Otherwise the box silently disagrees with the hardware until the scan
    reaches its next iteration."""
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl, _ = _viewController(appliesNow=False)

    ViewController._onDetectorSelectionToggled(ctrl, 'APD', False)

    assert ctrl._widget.pending['APD'] is True


def test_the_model_applying_a_change_clears_the_pending_mark():
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl, _ = _viewController(appliesNow=False)
    ViewController._onDetectorSelectionToggled(ctrl, 'APD', False)

    ViewController._onDetectorSelectionApplied(ctrl, 'APD', False)

    assert ctrl._widget.checked['APD'] is False
    assert ctrl._widget.pending['APD'] is False


def test_a_rejected_change_snaps_the_box_back_to_the_model():
    from imswitch.imcontrol.controller.controllers.ViewController import (
        ViewController,
    )
    ctrl, manager = _viewController()

    def refuse(name, selected):
        raise ValueError('not selectable')

    manager.setDetectorSelected = refuse
    ViewController._onDetectorSelectionToggled(ctrl, 'APD', False)

    assert ctrl._widget.checked['APD'] is True


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

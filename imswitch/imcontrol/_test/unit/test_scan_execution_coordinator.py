"""ScanExecutionCoordinator: exactly-once scan-iteration lifecycle (Phase 3).

Covers the five terminations a scan iteration can take — busy refusal, build
failure, arm exception, normal completion, abort — and the rule that
``finishScan`` runs for every participant regardless of refcount while the
hardware stop happens only at aggregate zero.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers._scan_execution import (
    EXCLUDED_KEY, FINISH_ABORT, FINISH_GRACEFUL, PARTICIPANTS_KEY,
    ScanExecutionCoordinator,
)
from imswitch.imcontrol.model.managers.NidaqManager import ScanBusyError


class _Detector:
    def __init__(self, name, isScanDriven=False):
        self.name = name
        self.isScanDriven = isScanDriven
        self.finishCalls = []
        # Real scan-driven managers finish asynchronously; flip this to model
        # a detector whose final read is still in flight.
        self.autoAcknowledge = True
        self.pendingAck = None

    def finishScan(self, mode, acknowledge):
        self.finishCalls.append(mode)
        if self.autoAcknowledge:
            acknowledge()
        else:
            self.pendingAck = acknowledge


class _DetectorsManager:
    def __init__(self, detectors, faulted=()):
        self._detectors = {d.name: d for d in detectors}
        self._faulted = set(faulted)
        self.acquired = []
        self.released = []
        self._seq = 0

    def getAllDeviceNames(self, condition=None):
        return [name for name, d in self._detectors.items()
                if condition is None or condition(d)]

    def isDetectorFaulted(self, name):
        return name in self._faulted

    def execOn(self, name, func):
        return func(self._detectors[name])

    def acquire(self, detectorNames, purpose):
        self._seq += 1
        handle = f'lease-{self._seq}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)

    def __getitem__(self, name):
        return self._detectors[name]


class _Nidaq:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def runScan(self, signalDict, scanInfoDict):
        self.calls.append((signalDict, dict(scanInfoDict)))
        if self._error is not None:
            raise self._error


def _setup(nidaqError=None, faulted=()):
    detectors = [
        _Detector('APD', isScanDriven=True),
        _Detector('TimeTagger', isScanDriven=True),
        _Detector('CAM', isScanDriven=False),
    ]
    manager = _DetectorsManager(detectors, faulted=faulted)
    nidaq = _Nidaq(error=nidaqError)
    return ScanExecutionCoordinator(manager, nidaq), manager, nidaq


# --------------------------------------------------------------------------- #
# Participant composition                                                      #
# --------------------------------------------------------------------------- #

def test_only_scan_driven_detectors_participate():
    coordinator, _, _ = _setup()

    assert coordinator.composeParticipants() == ['APD', 'TimeTagger']


def test_faulted_detectors_are_excluded_from_the_snapshot():
    """A broken point detector must never rejoin a scan."""
    coordinator, _, _ = _setup(faulted=['APD'])

    assert coordinator.composeParticipants() == ['TimeTagger']


# --------------------------------------------------------------------------- #
# Arming                                                                       #
# --------------------------------------------------------------------------- #

def test_arm_takes_one_scan_lease_over_the_snapshot():
    coordinator, manager, _ = _setup()

    coordinator.arm({}, {})

    assert manager.acquired == [
        (('APD', 'TimeTagger'), LeasePurpose.SCAN, 'lease-1')
    ]


def test_arm_injects_participants_into_scan_info_before_running():
    """The detectors' gate reads this off the dict sigScanBuilt carries, so it
    must be present by the time runScan is called."""
    coordinator, _, nidaq = _setup()
    scanInfoDict = {}

    coordinator.arm({'sig': 1}, scanInfoDict)

    assert scanInfoDict[PARTICIPANTS_KEY] == ['APD', 'TimeTagger']
    _, seenScanInfo = nidaq.calls[0]
    assert seenScanInfo[PARTICIPANTS_KEY] == ['APD', 'TimeTagger']


def test_arm_publishes_excluded_scan_driven_detectors_for_the_simulator():
    """The simulator is built from setupInfo alone and cannot tell a
    scan-driven detector from a camera, so the exclusion list is published
    explicitly rather than derived."""
    coordinator, _, _ = _setup(faulted=['APD'])
    scanInfoDict = {}

    coordinator.arm({}, scanInfoDict)

    assert scanInfoDict[PARTICIPANTS_KEY] == ['TimeTagger']
    assert scanInfoDict[EXCLUDED_KEY] == ['APD']


def test_nothing_is_excluded_when_every_scan_driven_detector_participates():
    coordinator, _, _ = _setup()
    scanInfoDict = {}

    coordinator.arm({}, scanInfoDict)

    assert scanInfoDict[EXCLUDED_KEY] == []


def test_arm_with_no_participants_takes_no_lease():
    """acquire() rejects an empty selection, so a scan with no scan-driven
    detectors must not try to take one."""
    manager = _DetectorsManager([_Detector('CAM')])
    coordinator = ScanExecutionCoordinator(manager, _Nidaq())

    scanInfoDict = {}
    coordinator.arm({}, scanInfoDict)

    assert manager.acquired == []
    assert scanInfoDict[PARTICIPANTS_KEY] == []


# --------------------------------------------------------------------------- #
# Exactly-once completion, all five terminations                               #
# --------------------------------------------------------------------------- #

def test_normal_completion_finishes_gracefully_and_releases():
    coordinator, manager, _ = _setup()
    token = coordinator.arm({}, {})

    assert coordinator.resolve(token) is True

    assert manager['APD'].finishCalls == [FINISH_GRACEFUL]
    assert manager['TimeTagger'].finishCalls == [FINISH_GRACEFUL]
    assert manager.released == ['lease-1']
    assert coordinator.activeToken is None


def test_abort_finishes_abruptly_and_releases():
    coordinator, manager, _ = _setup()
    token = coordinator.arm({}, {})

    coordinator.resolve(token, FINISH_ABORT)

    assert manager['APD'].finishCalls == [FINISH_ABORT]
    assert manager.released == ['lease-1']


def test_busy_refusal_unwinds_the_lease_and_resolves_the_token():
    """R7-1: runScan refused, nothing armed, no lifecycle signal will follow —
    ownership must not be stranded."""
    coordinator, manager, _ = _setup(nidaqError=ScanBusyError('busy'))

    with pytest.raises(ScanBusyError):
        coordinator.arm({}, {})

    assert manager.released == ['lease-1']
    assert coordinator.activeToken is None
    assert manager['APD'].finishCalls == [FINISH_ABORT]


def test_arm_exception_unwinds_the_lease():
    coordinator, manager, _ = _setup(nidaqError=RuntimeError('build blew up'))

    with pytest.raises(RuntimeError):
        coordinator.arm({}, {})

    assert manager.released == ['lease-1']
    assert coordinator.activeToken is None


def test_completion_runs_exactly_once_however_many_paths_fire():
    """sigScanDone, an abort click and a build-failure handler can all land for
    the same iteration; only the first may do the work."""
    coordinator, manager, _ = _setup()
    token = coordinator.arm({}, {})

    assert coordinator.resolve(token, FINISH_GRACEFUL) is True
    assert coordinator.resolve(token, FINISH_ABORT) is False
    assert coordinator.resolveActive(FINISH_ABORT) is False

    assert manager['APD'].finishCalls == [FINISH_GRACEFUL]  # not re-finished
    assert manager.released == ['lease-1']                  # not double-released


def test_resolve_active_completes_the_in_flight_iteration():
    """The sigScanDone handler only sees a signal, not the token."""
    coordinator, manager, _ = _setup()
    coordinator.arm({}, {})

    assert coordinator.resolveActive() is True
    assert manager.released == ['lease-1']


def test_resolve_of_an_unarmed_coordinator_is_harmless():
    coordinator, manager, _ = _setup()

    assert coordinator.resolveActive() is False
    assert manager.released == []


# --------------------------------------------------------------------------- #
# finishScan vs. refcount                                                      #
# --------------------------------------------------------------------------- #

def test_finish_scan_runs_even_when_another_lease_keeps_the_detector_armed():
    """A TimeTagger holding WORKFLOW+SCAN goes 2->1 on release: no hardware
    stop, but it still needs its final read."""
    coordinator, manager, _ = _setup()
    token = coordinator.arm({}, {})

    coordinator.resolve(token)

    # finishScan is driven by the snapshot, not by what release() stopped.
    assert manager['TimeTagger'].finishCalls == [FINISH_GRACEFUL]


def test_a_failing_finish_scan_does_not_block_the_others_or_the_release():
    coordinator, manager, _ = _setup()

    def explode(mode):
        raise RuntimeError('final read failed')

    manager['APD'].finishScan = explode
    token = coordinator.arm({}, {})

    coordinator.resolve(token)

    assert manager['TimeTagger'].finishCalls == [FINISH_GRACEFUL]
    assert manager.released == ['lease-1']


def test_etsted_runner_arms_through_the_coordinator_when_given_one():
    """The 5th entry point must get the same snapshot/lease/token as the four
    scan controllers, not a bare runScan."""
    from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import (
        EtSTEDTriggeredScanRunner,
    )

    coordinator, manager, nidaq = _setup()
    runner = EtSTEDTriggeredScanRunner()

    result = runner.trigger(
        runner.scan_widget_mode,
        nidaq_manager=nidaq,
        signal_dict={},
        scan_info_dict={},
        scan_coordinator=coordinator,
    )

    assert result.success is True
    assert manager.acquired == [
        (('APD', 'TimeTagger'), LeasePurpose.SCAN, 'lease-1')
    ]
    assert coordinator.activeToken is not None

    coordinator.resolveActive()
    assert manager.released == ['lease-1']


def test_every_nidaq_scan_entry_point_is_routed_through_the_coordinator():
    """There are five direct runScan callers; a new one must not quietly skip
    the lifecycle. Source-level guard, since constructing the controllers needs
    the full Qt stack."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    pattern = re.compile(r'(?<!def )runScan\(signal|nidaqManager\.runScan\(|'
                         r'nidaq_manager\.runScan\(')

    offenders = []
    for path in (root / 'imswitch').rglob('*.py'):
        if '_test' in path.parts:
            continue
        if path.name in ('NidaqManager.py', '_scan_execution.py'):
            continue  # defines runScan / is the coordinator
        for number, line in enumerate(
                path.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith('#'):
                continue
            if pattern.search(line):
                offenders.append(f'{path.relative_to(root)}:{number}')

    # EtSTEDTriggeredScanRunner keeps a documented no-coordinator fallback.
    offenders = [o for o in offenders
                 if not o.startswith('imswitch/imcontrol/model/EtSTEDTriggeredScanRunner.py')]

    assert offenders == [], (
        'These call nidaqManager.runScan directly instead of arming through '
        'ScanExecutionCoordinator, so their scan iteration gets no participant '
        'snapshot, no SCAN lease and no exactly-once completion: '
        + ', '.join(offenders)
    )


# --------------------------------------------------------------------------- #
# Asynchronous finish barrier                                                  #
# --------------------------------------------------------------------------- #

def test_lease_is_held_until_every_participant_acknowledges():
    """The barrier: releasing while a detector is still reading out its final
    frame lets hardware teardown destroy the worker mid-read."""
    coordinator, manager, _ = _setup()
    manager['TimeTagger'].autoAcknowledge = False
    token = coordinator.arm({}, {})

    coordinator.resolve(token)

    assert manager.released == []          # TimeTagger has not finished
    assert token.finishing is True
    assert token.resolved is False
    assert coordinator.activeToken is token

    manager['TimeTagger'].pendingAck()

    assert manager.released == ['lease-1']
    assert token.resolved is True
    assert coordinator.activeToken is None


def test_completion_callback_fires_only_after_the_barrier_clears():
    """Repeat re-arm hangs off this, so the next frame cannot start while the
    previous frame's data is still being read out."""
    coordinator, manager, _ = _setup()
    manager['TimeTagger'].autoAcknowledge = False
    token = coordinator.arm({}, {})
    completed = []

    coordinator.resolve(token, onComplete=lambda: completed.append(True))
    assert completed == []

    manager['TimeTagger'].pendingAck()
    assert completed == [True]


def test_resolve_does_not_block_waiting_for_acknowledgements():
    coordinator, manager, _ = _setup()
    manager['APD'].autoAcknowledge = False
    manager['TimeTagger'].autoAcknowledge = False
    token = coordinator.arm({}, {})

    assert coordinator.resolve(token) is True  # returns immediately


def test_a_second_resolve_while_finishing_is_rejected():
    coordinator, manager, _ = _setup()
    manager['TimeTagger'].autoAcknowledge = False
    token = coordinator.arm({}, {})

    assert coordinator.resolve(token) is True
    assert coordinator.resolve(token, FINISH_ABORT) is False
    assert manager['TimeTagger'].finishCalls == [FINISH_GRACEFUL]


def test_acknowledging_twice_does_not_double_release():
    coordinator, manager, _ = _setup()
    manager['TimeTagger'].autoAcknowledge = False
    token = coordinator.arm({}, {})
    coordinator.resolve(token)

    ack = manager['TimeTagger'].pendingAck
    ack()
    ack()

    assert manager.released == ['lease-1']


def test_a_detector_that_never_acknowledges_is_timed_out():
    """A manager that never acks (mock mode, dead worker) must not wedge
    scanning forever — losing a final frame beats a stuck GUI."""
    scheduled = []
    detectors = [_Detector('APD', isScanDriven=True)]
    manager = _DetectorsManager(detectors)
    coordinator = ScanExecutionCoordinator(
        manager, _Nidaq(),
        scheduleTimeout=lambda delayS, cb: scheduled.append((delayS, cb)),
    )
    manager['APD'].autoAcknowledge = False
    token = coordinator.arm({}, {})
    completed = []

    coordinator.resolve(token, onComplete=lambda: completed.append(True))
    assert manager.released == []
    assert len(scheduled) == 1

    scheduled[0][1]()  # deadline fires

    assert token.timedOut is True
    assert manager.released == ['lease-1']
    assert completed == [True]


def test_a_late_acknowledgement_after_timeout_is_harmless():
    scheduled = []
    detectors = [_Detector('APD', isScanDriven=True)]
    manager = _DetectorsManager(detectors)
    coordinator = ScanExecutionCoordinator(
        manager, _Nidaq(),
        scheduleTimeout=lambda delayS, cb: scheduled.append((delayS, cb)),
    )
    manager['APD'].autoAcknowledge = False
    token = coordinator.arm({}, {})
    coordinator.resolve(token)
    scheduled[0][1]()

    manager['APD'].pendingAck()  # detector finally finishes

    assert manager.released == ['lease-1']  # not released twice


def test_a_manager_that_cannot_be_asked_does_not_hold_the_barrier():
    coordinator, manager, _ = _setup()

    def explode(mode, acknowledge):
        raise RuntimeError('finishScan blew up')

    manager['APD'].finishScan = explode
    token = coordinator.arm({}, {})

    coordinator.resolve(token)

    assert manager.released == ['lease-1']
    assert token.resolved is True


def test_repeat_iterations_get_independent_tokens_and_leases():
    coordinator, manager, _ = _setup()

    first = coordinator.arm({}, {})
    coordinator.resolve(first)
    second = coordinator.arm({}, {})
    coordinator.resolve(second)

    assert first is not second
    assert manager.released == ['lease-1', 'lease-2']
    assert manager['APD'].finishCalls == [FINISH_GRACEFUL, FINISH_GRACEFUL]


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

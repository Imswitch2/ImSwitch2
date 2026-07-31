"""Unit tests for the acquisition lease core (Phase 1).

A detector participates in ImSwitch-managed acquisition iff it holds >= 1
acquisition lease. These tests cover the refcount/transaction/fault semantics
of AcquisitionLeaseTable directly, without Qt or real detectors.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import pytest

from imswitch.imcontrol.model.managers._acquisition_leases import (
    AcquisitionLeaseTable, DetectorFaultedError, LeasePurpose,
)


class _Hardware:
    """Records start/stop calls; can be told to fail either operation."""

    def __init__(self, failStarts=(), failStops=()):
        self.started = []
        self.stopped = []
        self.armed = set()
        self._failStarts = set(failStarts)
        self._failStops = set(failStops)

    def start(self, name):
        self.started.append(name)
        if name in self._failStarts:
            raise RuntimeError(f'start failed: {name}')
        self.armed.add(name)

    def stop(self, name):
        self.stopped.append(name)
        if name in self._failStops:
            raise RuntimeError(f'stop failed: {name}')
        self.armed.discard(name)

    def failStopsFor(self, *names):
        self._failStops.update(names)

    def clearStopFailures(self):
        self._failStops.clear()


def makeTable(hardware, **kwargs):
    states = []

    def onStateChanged(name, leased, faulted):
        states.append((name, leased, faulted))

    table = AcquisitionLeaseTable(
        startDetector=hardware.start,
        stopDetector=hardware.stop,
        onStateChanged=onStateChanged,
        **kwargs,
    )
    return table, states


# --------------------------------------------------------------------------- #
# Refcounting                                                                  #
# --------------------------------------------------------------------------- #

def test_detector_arms_once_and_stops_on_last_release():
    hw = _Hardware()
    table, _ = makeTable(hw)

    first, _ = table.acquire(['A'], LeasePurpose.LIVE_VIEW)
    second, _ = table.acquire(['A'], LeasePurpose.RECORDING)
    assert hw.started == ['A']  # armed once, not twice

    table.release(first)
    assert hw.stopped == []  # still leased by RECORDING
    assert table.isLeased('A')

    table.release(second)
    assert hw.stopped == ['A']
    assert not table.isLeased('A')


def test_overlapping_leases_only_start_the_new_detectors():
    hw = _Hardware()
    table, _ = makeTable(hw)

    table.acquire(['A', 'B'], LeasePurpose.LIVE_VIEW)
    table.acquire(['B', 'C'], LeasePurpose.SCAN)

    assert hw.started == ['A', 'B', 'C']


def test_duplicate_names_within_one_lease_count_once():
    hw = _Hardware()
    table, _ = makeTable(hw)

    handle, _ = table.acquire(['A', 'A'], LeasePurpose.GENERIC)
    assert hw.started == ['A']

    table.release(handle)
    assert hw.stopped == ['A']
    assert not table.isLeased('A')


def test_empty_iterable_is_rejected_never_means_all():
    hw = _Hardware()
    table, _ = makeTable(hw)

    with pytest.raises(ValueError):
        table.acquire([], LeasePurpose.LIVE_VIEW)
    assert hw.started == []


def test_empty_allowed_only_for_the_legacy_shim():
    hw = _Hardware()
    table, _ = makeTable(hw)

    handle, _ = table.acquire([], LeasePurpose.GENERIC, allowEmpty=True)
    assert hw.started == []
    table.release(handle)


def test_release_rejects_unknown_or_reused_handle():
    hw = _Hardware()
    table, _ = makeTable(hw)

    handle, _ = table.acquire(['A'], LeasePurpose.GENERIC)
    table.release(handle)
    with pytest.raises(ValueError):
        table.release(handle)


def test_handle_fields_are_immutable():
    hw = _Hardware()
    table, _ = makeTable(hw)
    handle, _ = table.acquire(['A'], LeasePurpose.GENERIC)

    with pytest.raises(AttributeError):
        handle.purpose = LeasePurpose.LIVE_VIEW
    with pytest.raises(AttributeError):
        handle.detectorNames = ('B',)

    assert table.isActiveHandle(handle)
    table.release(handle)
    assert not table.isActiveHandle(handle)


def test_purpose_must_be_a_lease_purpose():
    hw = _Hardware()
    table, _ = makeTable(hw)

    with pytest.raises(TypeError):
        table.acquire(['A'], 'liveView')


# --------------------------------------------------------------------------- #
# Transactional acquire                                                        #
# --------------------------------------------------------------------------- #

def test_failed_acquire_rolls_back_existing_and_new_detectors():
    """existing-A + new-B + failing-C: A must go 2->1 (stay armed), B must be
    stopped, and failing-C must receive a compensating stop because its start
    could have partially armed hardware before raising."""
    hw = _Hardware(failStarts=['C'])
    table, _ = makeTable(hw)

    keepA, _ = table.acquire(['A'], LeasePurpose.LIVE_VIEW)
    hw.started.clear()

    with pytest.raises(RuntimeError):
        table.acquire(['A', 'B', 'C'], LeasePurpose.SCAN)

    assert hw.started == ['B', 'C']
    assert hw.stopped == ['C', 'B']
    assert table.isLeased('A')       # 2 -> 1, still held by the LIVE_VIEW lease
    assert not table.isLeased('B')
    assert not table.isLeased('C')
    assert len(table.activeLeases()) == 1

    table.release(keepA)
    assert hw.stopped == ['C', 'B', 'A']


def test_rollback_stop_failure_faults_that_detector_and_reraises_original():
    hw = _Hardware(failStarts=['C'], failStops=['B'])
    table, _ = makeTable(hw)

    with pytest.raises(RuntimeError, match='start failed: C'):
        table.acquire(['B', 'C'], LeasePurpose.SCAN)

    assert table.isFaulted('B')
    assert not table.isLeased('B')


def test_failed_start_is_compensated_and_quarantined_if_stop_fails():
    events = []

    def partialStart(name):
        events.append(('start', name))
        raise RuntimeError(f'partially started: {name}')

    def failedCompensation(name):
        events.append(('stop', name))
        raise RuntimeError(f'cannot stop: {name}')

    table = AcquisitionLeaseTable(
        startDetector=partialStart,
        stopDetector=failedCompensation,
    )

    with pytest.raises(RuntimeError, match='partially started: A'):
        table.acquire(['A'], LeasePurpose.SCAN)

    assert events == [('start', 'A'), ('stop', 'A')]
    assert not table.isLeased('A')
    assert table.isFaulted('A')
    with pytest.raises(DetectorFaultedError):
        table.acquire(['A'], LeasePurpose.SCAN)


def test_acquire_leaves_no_lease_behind_when_it_fails():
    hw = _Hardware(failStarts=['B'])
    table, _ = makeTable(hw)

    with pytest.raises(RuntimeError):
        table.acquire(['A', 'B'], LeasePurpose.SCAN)

    assert table.activeLeases() == []
    assert not table.isLeased('A')


# --------------------------------------------------------------------------- #
# Fault quarantine                                                             #
# --------------------------------------------------------------------------- #

def test_failed_stop_faults_and_quarantines_the_detector():
    hw = _Hardware(failStops=['A'])
    table, _ = makeTable(hw)

    handle, _ = table.acquire(['A'], LeasePurpose.SCAN)
    transition = table.release(handle)

    assert transition.newlyFaulted == ['A']
    assert transition.stopped == []
    assert table.isFaulted('A')
    assert table.faultedDetectors() == ['A']


def test_faulted_detector_cannot_be_reacquired():
    hw = _Hardware(failStops=['A'])
    table, _ = makeTable(hw)

    handle, _ = table.acquire(['A'], LeasePurpose.SCAN)
    table.release(handle)

    with pytest.raises(DetectorFaultedError):
        table.acquire(['A'], LeasePurpose.LIVE_VIEW)
    # A broken detector must not be silently re-armed as part of a group.
    with pytest.raises(DetectorFaultedError):
        table.acquire(['B', 'A'], LeasePurpose.SCAN)
    assert not table.isLeased('B')


def test_release_of_a_faulted_detector_does_not_retry_the_stop():
    hw = _Hardware(failStops=['A'])
    table, _ = makeTable(hw)

    first, _ = table.acquire(['A'], LeasePurpose.SCAN)
    second, _ = table.acquire(['A'], LeasePurpose.RECORDING)
    table.release(first)
    assert hw.stopped == []  # refcount still 1, no stop attempted

    table.release(second)          # 1 -> 0: stop attempted, fails, faults
    assert hw.stopped == ['A']
    assert table.isFaulted('A')

    hw.stopped.clear()
    with pytest.raises(DetectorFaultedError):
        table.acquire(['A'], LeasePurpose.SCAN)
    assert hw.stopped == []  # quarantine, not a natural 1 -> 0 retry


def test_retry_stop_recovers_a_faulted_detector():
    hw = _Hardware(failStops=['A'])
    table, _ = makeTable(hw)

    handle, _ = table.acquire(['A'], LeasePurpose.SCAN)
    table.release(handle)
    assert table.isFaulted('A')

    with pytest.raises(RuntimeError):
        table.retryStop('A')       # still failing -> still quarantined
    assert table.isFaulted('A')

    hw.clearStopFailures()
    table.retryStop('A')
    assert not table.isFaulted('A')
    table.acquire(['A'], LeasePurpose.LIVE_VIEW)  # usable again


def test_retry_stop_on_a_healthy_detector_is_a_no_op():
    hw = _Hardware()
    table, _ = makeTable(hw)

    table.retryStop('A')
    assert hw.stopped == []


# --------------------------------------------------------------------------- #
# Mirrors                                                                      #
# --------------------------------------------------------------------------- #

def test_state_changes_are_reported_for_mirroring():
    hw = _Hardware(failStops=['A'])
    table, states = makeTable(hw)

    handle, _ = table.acquire(['A'], LeasePurpose.SCAN)
    assert ('A', True, False) in states

    states.clear()
    table.release(handle)
    assert states == [('A', False, False), ('A', False, True)]


def test_state_change_precedes_hardware_start():
    """The mirror must already read 'leased' when startAcquisition() runs, so a
    manager consulting its own mirror while arming sees the truth."""
    hw = _Hardware()
    order = []

    def start(name):
        order.append(('start', name))
        hw.start(name)

    table = AcquisitionLeaseTable(
        startDetector=start,
        stopDetector=hw.stop,
        onStateChanged=lambda name, leased, faulted: order.append(
            ('mirror', name, leased)),
    )
    table.acquire(['A'], LeasePurpose.SCAN)

    assert order == [('mirror', 'A', True), ('start', 'A')]


# --------------------------------------------------------------------------- #
# Transitions: global signals and frame-stream membership                      #
# --------------------------------------------------------------------------- #

def test_focus_leases_do_not_assert_the_global_acquisition_signals():
    hw = _Hardware()
    table, _ = makeTable(hw)

    _, focusTransition = table.acquire(['F'], LeasePurpose.FOCUS)
    assert not focusTransition.nonFocusFirst

    _, liveTransition = table.acquire(['A'], LeasePurpose.LIVE_VIEW)
    assert liveTransition.nonFocusFirst  # first user-visible acquisition


def test_permanent_focus_lease_does_not_keep_signals_asserted():
    hw = _Hardware()
    table, _ = makeTable(hw)

    table.acquire(['F'], LeasePurpose.FOCUS)          # startup lease, stays
    live, _ = table.acquire(['A'], LeasePurpose.LIVE_VIEW)

    transition = table.release(live)
    assert transition.nonFocusLast  # stopped, despite the FOCUS lease


def test_frame_stream_first_marks_poll_thread_startup():
    """Only a 'first' flag exists: shutdown is decided by the caller from
    frameStreamHandles() before it takes the lock, never from a transition
    handed back by release()."""
    hw = _Hardware()
    table, _ = makeTable(hw)

    first, t1 = table.acquire(['A'], LeasePurpose.LIVE_VIEW)
    assert t1.frameStreamFirst
    second, t2 = table.acquire(['B'], LeasePurpose.LIVE_VIEW)
    assert not t2.frameStreamFirst

    table.release(second)
    assert len(table.frameStreamHandles()) == 1   # still streaming
    table.release(first)
    assert table.frameStreamHandles() == []       # caller may stop the thread


def test_event_stream_lease_keeps_the_poll_thread_running():
    """R6-1: with live view off, an EVENT_STREAM lease must keep the poll
    thread alive — otherwise the detector is armed but never read and the
    event-detection loop stalls."""
    hw = _Hardware()
    table, _ = makeTable(hw)

    live, _ = table.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    _, eventTransition = table.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    assert not eventTransition.frameStreamFirst  # already streaming

    # Live view off, event loop still running: the thread must NOT stop.
    table.release(live)
    assert len(table.frameStreamHandles()) == 1


def test_event_stream_alone_starts_the_poll_thread():
    hw = _Hardware()
    table, _ = makeTable(hw)

    handle, transition = table.acquire(['FAST'], LeasePurpose.EVENT_STREAM)

    assert transition.frameStreamFirst
    table.release(handle)
    assert table.frameStreamHandles() == []


def test_non_streaming_purposes_do_not_drive_the_poll_thread():
    """EVENT_DIRECT reads frames itself; SCAN/RECORDING detectors are armed
    but not polled into sigImageUpdated."""
    hw = _Hardware()
    table, _ = makeTable(hw)

    for purpose in (LeasePurpose.EVENT_DIRECT, LeasePurpose.SCAN,
                    LeasePurpose.RECORDING, LeasePurpose.FOCUS):
        handle, transition = table.acquire(['D'], purpose)
        assert not transition.frameStreamFirst, purpose
        assert table.frameStreamHandles() == [], purpose
        table.release(handle)


def test_frame_stream_membership_is_live_view_union_event_stream():
    hw = _Hardware()
    table, _ = makeTable(hw)

    table.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    table.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    table.acquire(['APD'], LeasePurpose.SCAN)
    table.acquire(['DIRECT'], LeasePurpose.EVENT_DIRECT)

    membership = table.leasedDetectorNames(
        [LeasePurpose.LIVE_VIEW, LeasePurpose.EVENT_STREAM]
    )
    assert membership == {'CAM', 'FAST'}
    assert table.leasedDetectorNames() == {'CAM', 'FAST', 'APD', 'DIRECT'}


def test_frame_stream_handles_are_queryable_for_thread_lifecycle():
    """Callers decide poll-thread lifecycle OUTSIDE the lock using this — the
    poll loop takes the same lock, so joining the thread while holding it
    deadlocks."""
    hw = _Hardware()
    table, _ = makeTable(hw)

    table.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
    table.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
    table.acquire(['APD'], LeasePurpose.SCAN)

    assert len(table.frameStreamHandles()) == 2


def test_table_exposes_no_hook_that_runs_under_the_lock_besides_mirroring():
    """The 'before stops' hook is gone on purpose.

    The DetectorsManager used it to join the frame-stream poll thread, which
    deadlocked once the poll loop started reading membership from this table:
    the joiner held the lock the poll was blocked on. Only onStateChanged —
    which just writes two attributes — may run under the lock.
    """
    import inspect

    params = inspect.signature(AcquisitionLeaseTable.__init__).parameters
    assert 'onBeforeStops' not in params
    assert set(params) - {'self'} == {
        'startDetector', 'stopDetector', 'onStateChanged'
    }


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

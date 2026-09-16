"""The positioning handshake between a recording session and a stage owner.

Four terminals, one-way, first one wins. The asymmetry matters: a session may
only arm on RESOLVED, so anything else must be distinguishable from it and from
"still moving".
"""

import threading
import time

from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningOutcome,
    PositioningRequest,
)


def test_a_fresh_request_is_pending_and_may_not_proceed():
    request = PositioningRequest(index=0, timeout_s=10)

    assert request.outcome is PositioningOutcome.PENDING
    assert not request.settled
    assert not request.mayProceed


def test_resolving_is_the_only_outcome_that_permits_acquiring():
    resolved = PositioningRequest(0, timeout_s=10)
    resolved.resolve()
    failed = PositioningRequest(1, timeout_s=10)
    failed.fail('stage refused')
    cancelled = PositioningRequest(2, timeout_s=10)
    cancelled.cancel()

    assert resolved.mayProceed
    assert not failed.mayProceed
    assert not cancelled.mayProceed
    assert all(r.settled for r in (resolved, failed, cancelled))


def test_a_request_expires_on_its_own_without_a_timer():
    """A stage that never reports must not wedge the session forever."""
    request = PositioningRequest(0, timeout_s=0.05)

    assert request.outcome is PositioningOutcome.PENDING
    time.sleep(0.08)

    assert request.outcome is PositioningOutcome.TIMED_OUT
    assert not request.mayProceed
    assert 'did not report position 0' in request.message


def test_the_first_terminal_wins():
    """A late resolve cannot revive a run the operator stopped."""
    request = PositioningRequest(0, timeout_s=10)

    assert request.cancel('operator stopped the run') is True
    assert request.resolve() is False

    assert request.outcome is PositioningOutcome.CANCELLED
    assert not request.mayProceed


def test_an_expired_request_cannot_be_resolved_afterwards():
    request = PositioningRequest(0, timeout_s=0.02)
    time.sleep(0.05)
    assert request.outcome is PositioningOutcome.TIMED_OUT

    assert request.resolve() is False
    assert request.outcome is PositioningOutcome.TIMED_OUT


def test_waiting_returns_as_soon_as_the_worker_resolves():
    request = PositioningRequest(0, timeout_s=5)

    def move():
        time.sleep(0.02)
        request.resolve()

    worker = threading.Thread(target=move, daemon=True)
    started = time.monotonic()
    worker.start()
    outcome = request.wait()
    elapsed = time.monotonic() - started
    worker.join()

    assert outcome is PositioningOutcome.RESOLVED
    assert elapsed < 1.0, 'wait did not return on resolution'


def test_the_failure_message_survives_to_the_session():
    request = PositioningRequest(3, timeout_s=10)
    request.fail('positioner Z is faulted')

    assert request.outcome is PositioningOutcome.FAILED
    assert request.message == 'positioner Z is faulted'

"""The positioning gate: a lapse point arms only once the stage is there.

The whole reason it is a gate and not a call is threading. Timepoints advance
from Qt timers on the GUI thread while the stage is driven from a worker, so
the gate must hand control back to the event loop while a move is in flight and
be re-entered later — never block it.
"""

import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)
from imswitch.imcontrol.model.managers.RecordingManager import FailureKind
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)


class _Timer:
    """Records that a retry was scheduled instead of the loop being blocked."""

    scheduled = []

    def __init__(self, singleShot=False):
        self._singleShot = singleShot
        self.timeout = SimpleNamespace(connect=lambda _fn: None)

    def start(self, interval):
        _Timer.scheduled.append(interval)


def _controller(provider, monkeypatch, lapseCurrent=0):
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.RecordingController.Timer',
        _Timer,
    )
    _Timer.scheduled = []
    failures = []
    controller = SimpleNamespace(
        _positioningProvider=provider,
        _pendingPositioning=None,
        lapseCurrent=lapseCurrent,
        # The retry reconnects the timer to this; only its existence matters.
        nextLapse=lambda: None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )
    return controller, failures


def test_a_resolved_request_lets_the_point_arm(monkeypatch):
    request = PositioningRequest(0, timeout_s=10)
    request.resolve()
    ctrl, failures = _controller(lambda _i: request, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is True
    assert failures == []
    assert _Timer.scheduled == [], 'no retry needed once settled'


def test_a_pending_request_yields_the_event_loop_and_retries(monkeypatch):
    """Still moving: come back later rather than hold the GUI thread."""
    request = PositioningRequest(0, timeout_s=10)
    ctrl, failures = _controller(lambda _i: request, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is False
    assert failures == []
    assert len(_Timer.scheduled) == 1
    # The same request is kept, so the provider is asked exactly once.
    assert ctrl._pendingPositioning is request


@pytest.mark.parametrize('settle, expected', [
    (lambda r: r.fail('positioner faulted'), 'failed'),
    (lambda r: r.cancel('operator stopped the run'), 'cancelled'),
])
def test_a_request_that_did_not_resolve_never_arms(settle, expected,
                                                   monkeypatch):
    request = PositioningRequest(2, timeout_s=10)
    settle(request)
    ctrl, failures = _controller(lambda _i: request, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is False
    assert len(failures) == 1
    message, kwargs = failures[0]
    assert expected in message
    assert kwargs['kind'] is FailureKind.HARDWARE


def test_a_timed_out_request_never_arms(monkeypatch):
    """A stage that never reports must not wedge the session."""
    request = PositioningRequest(0, timeout_s=0.01)
    time.sleep(0.03)
    ctrl, failures = _controller(lambda _i: request, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is False
    assert 'timed-out' in failures[0][0]


def test_a_provider_that_raises_stops_the_session(monkeypatch):
    def broken(_index):
        raise RuntimeError('stage unreachable')

    ctrl, failures = _controller(broken, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is False
    assert 'stage unreachable' in failures[0][0]
    assert failures[0][1]['kind'] is FailureKind.HARDWARE


def test_a_provider_may_decline_to_position_a_point(monkeypatch):
    """Returning None means 'nothing to do here', not 'failed'."""
    ctrl, failures = _controller(lambda _i: None, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is True
    assert failures == []


def test_a_lapse_without_a_provider_is_unaffected(monkeypatch):
    ctrl, failures = _controller(None, monkeypatch)

    assert RecordingController._awaitPositioning(ctrl) is True
    assert failures == []
    assert _Timer.scheduled == []


def test_the_provider_is_asked_for_the_point_being_started(monkeypatch):
    asked = []

    def provider(index):
        asked.append(index)
        request = PositioningRequest(index, timeout_s=10)
        request.resolve()
        return request

    ctrl, _failures = _controller(provider, monkeypatch, lapseCurrent=7)

    assert RecordingController._awaitPositioning(ctrl) is True
    assert asked == [7]

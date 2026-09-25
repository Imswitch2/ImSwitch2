"""Unit tests for the cooperative cancel token (no Qt needed)."""
import threading

import pytest

from imswitch.imcommon.model.cancellation import (
    CancelToken, OperationCancelled, cancellableSleep, checkpoint,
    clearCurrentCancelToken, currentCancelToken, setCurrentCancelToken,
)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeInvocation:
    def __init__(self):
        self.epoch = 0
        self.cancelled = False

    def cancelPending(self):
        self.cancelled = True
        return True


def test_idle_checkpoint_is_a_noop():
    token = CancelToken()
    token.checkpoint()
    assert token.state == CancelToken.IDLE
    assert not token.isStopRequested()


def test_first_checkpoint_after_request_raises_once_then_cleanup_window_is_quiet():
    clock = FakeClock()
    token = CancelToken(cleanupBudgetS=30, clock=clock)
    assert token.requestStop() is True
    assert token.requestStop() is False  # idempotent
    assert token.state == CancelToken.REQUESTED
    with pytest.raises(OperationCancelled):
        token.checkpoint()
    assert token.state == CancelToken.DELIVERED
    # cleanup window: no further raises
    for _ in range(5):
        token.checkpoint()
    assert token.currentEpoch() == 1


def test_cleanup_budget_expiry_rearms_cancellation():
    clock = FakeClock()
    token = CancelToken(cleanupBudgetS=10, clock=clock)
    token.requestStop()
    with pytest.raises(OperationCancelled):
        token.checkpoint()
    clock.now += 9.9
    token.checkpoint()  # still inside the budget
    clock.now += 0.2
    assert token.state == CancelToken.EXPIRED
    with pytest.raises(OperationCancelled):
        token.checkpoint()
    with pytest.raises(OperationCancelled):
        token.checkpoint()  # and again: expired stays expired
    assert token.isExpired()


def test_request_stop_clamps_the_cleanup_budget_only_downwards():
    token = CancelToken(cleanupBudgetS=30)
    token.requestStop(cleanupBudgetS=3)
    assert token.cleanupBudgetS == 3
    token2 = CancelToken(cleanupBudgetS=3)
    token2.requestStop(cleanupBudgetS=30)
    assert token2.cleanupBudgetS == 3


def test_mark_delivered_covers_the_injected_route():
    token = CancelToken()
    assert token.markDelivered() is False  # nothing requested
    token.requestStop()
    assert token.markDelivered() is True
    assert token.state == CancelToken.DELIVERED
    token.checkpoint()  # cleanup window, no raise


def test_expire_now_forces_expired_even_from_idle():
    token = CancelToken()
    token.expireNow()
    assert token.isExpired()
    with pytest.raises(OperationCancelled):
        token.checkpoint()


def test_request_stop_cancels_the_pending_epoch0_invocation_synchronously():
    token = CancelToken()
    inv = FakeInvocation()
    assert token.openInvocation(inv) == 0
    token.requestStop()
    assert inv.cancelled is True
    assert token.admit(inv) is False


def test_cleanup_invocation_gets_epoch1_and_is_admitted_only_in_the_window():
    clock = FakeClock()
    token = CancelToken(cleanupBudgetS=5, clock=clock)
    token.requestStop()
    with pytest.raises(OperationCancelled):
        token.openInvocation(FakeInvocation())  # REQUESTED: deliver, dispatch nothing
    inv = FakeInvocation()
    assert token.openInvocation(inv) == 1
    assert token.admit(inv) is True
    stale = FakeInvocation()  # epoch 0 leftover must never be admitted now
    assert token.admit(stale) is False
    clock.now += 6
    with pytest.raises(OperationCancelled):
        token.openInvocation(FakeInvocation())  # EXPIRED: refuse before dispatch
    assert token.admit(inv) is False
    token.closeInvocation(inv)


def test_thread_local_token_accessors():
    clearCurrentCancelToken()
    assert currentCancelToken() is None
    checkpoint()  # no token: no-op
    token = CancelToken()
    setCurrentCancelToken(token)
    assert currentCancelToken() is token
    seen = {}

    def other():
        seen['other'] = currentCancelToken()

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen['other'] is None  # thread-local
    clearCurrentCancelToken()


def test_cancellable_sleep_honours_the_token():
    token = CancelToken()
    setCurrentCancelToken(token)
    try:
        cancellableSleep(0.01)  # completes normally
        token.requestStop()
        with pytest.raises(OperationCancelled):
            cancellableSleep(10)
    finally:
        clearCurrentCancelToken()

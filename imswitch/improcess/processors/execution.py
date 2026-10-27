"""One processor run that can be cancelled and can stream what it prints.

Qt-free, like :mod:`~imswitch.improcess.processors.run`, which it wraps: the GUI
runs it on a worker thread (:mod:`~imswitch.improcess.controller.processor_runner`),
and a test or a headless caller can run it anywhere.

Cancellation is the one ImScripting uses (``imcommon.model.cancellation``): a
:class:`CancelToken` published to the run's thread. A processor that loops can
call ``imswitch.imcommon.model.checkpoint()`` to stop at a point of its choosing;
one that does not is interrupted by the runner, which injects
``OperationCancelled`` into the thread after a grace period. That is a
``BaseException``, so an ``except Exception`` in the code being run cannot
swallow it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from imswitch.imcommon.model import (
    CancelToken,
    OperationCancelled,
    clearCurrentCancelToken,
    routeThisThreadsOutputTo,
    setCurrentCancelToken,
)
from imswitch.improcess.processors.run import run_processor


@dataclass(frozen=True)
class RunOutcome:
    """How a run ended: what it made, what failed, or that it was cancelled.

    A cancelled run has neither results nor failures: what it had made is
    discarded, because nothing half-finished should reach the results list.
    """

    results: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    cancelled: bool = False


class _CallbackSink:
    """Hands each piece of what the run prints to ``callback``."""

    def __init__(self, callback):
        self._callback = callback

    def write(self, text):
        if text:
            self._callback(text)
        return len(text)

    def flush(self):
        pass


class _UnroutedLogger:
    """A logger whose records go to the real stream even while the run's output is captured.

    The framework logs a failed run (``logger.exception``) from the run's own
    thread, and the log handler writes to ``stderr``: captured, the traceback
    would appear in the panel's output, above the one-line error the panel shows.
    """

    def __init__(self, logger):
        self._logger = logger

    def __getattr__(self, name):
        target = getattr(self._logger, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            with routeThisThreadsOutputTo(None):
                return target(*args, **kwargs)

        return call


def execute_run(processor, inputs, params: dict, logger, *, token=None, on_output=None) -> RunOutcome:
    """:func:`~imswitch.improcess.processors.run.run_processor` with cancel and output.

    ``token`` (a fresh one if not given) is published to this thread for the
    duration, so ``checkpoint()`` inside the processor honours it. ``on_output``,
    if given, is called with what the processor prints and writes to ``stderr``
    as it does so, from this thread, and only for this thread's output; without
    it the output goes where it always went.

    A run that was asked to stop is reported as cancelled even if it finished
    first: the person pressed Cancel and does not expect a result.
    """
    token = token if token is not None else CancelToken()
    setCurrentCancelToken(token)
    try:
        routed = (
            routeThisThreadsOutputTo(_CallbackSink(on_output))
            if on_output is not None
            else contextlib.nullcontext()
        )
        with routed:
            results, failures = run_processor(
                processor, inputs, params,
                _UnroutedLogger(logger) if on_output is not None else logger,
            )
    except OperationCancelled:
        return RunOutcome(cancelled=True)
    finally:
        clearCurrentCancelToken()
    if token.isStopRequested():
        return RunOutcome(cancelled=True)
    return RunOutcome(results=list(results), failures=list(failures))


__all__ = ["RunOutcome", "execute_run"]

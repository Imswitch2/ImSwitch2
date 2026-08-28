from __future__ import annotations

from typing import Callable,Protocol

from .job import CGHJob
from .result import CGHResult


class CGHExecutionHandle(Protocol):
    """Optional cancellation handle returned by a :class:`CGHExecutor`."""

    def cancel(self) -> None:
        ...


class CGHExecutor(Protocol):
    """Minimal host-independent contract for detached CGH execution.

    The executor controls the callback execution context. ``SLMSession`` is
    toolkit-independent; presentation adapters are responsible for additional
    thread marshalling when their executor does not already provide it.
    """

    def submit(
        self,
        job: CGHJob,
        on_result: Callable[[CGHResult],None],
        on_error: Callable[[Exception],None],
    ) -> CGHExecutionHandle | None:
        ...

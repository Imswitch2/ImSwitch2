"""Shared narrow infrastructure for focused feedback application services."""

from __future__ import annotations

from typing import Any,Callable,TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


class FeedbackComponent:
    """Common runtime/callback/editor capabilities without an owner/session locator."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
    ) -> None:
        self._runtime_provider = runtime_provider
        self._callbacks_provider = callbacks_provider
        self._editor_writes_allowed = editor_writes_allowed

    @property
    def _runtime(self) -> "SLMRuntime":
        return self._runtime_provider()

    @property
    def _callbacks(self) -> "SLMFeedbackCallbacks":
        return self._callbacks_provider()

    def _require_editor_mode(self) -> None:
        if not self._editor_writes_allowed():
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def _section_changed(self,section_key: str) -> None:
        self._callbacks.on_section_changed(section_key)

    def _feedback_state_changed(self,section_key: str) -> None:
        self._callbacks.on_feedback_state_changed(section_key)

    def _position_reference_changed(self,section_key: str) -> None:
        self._callbacks.on_position_reference_changed(section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._callbacks.on_warning(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self._callbacks.on_error(str(title),error)

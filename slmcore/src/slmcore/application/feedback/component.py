"""Temporary shared bridge used while feedback responsibilities are extracted.

Pass 2 keeps each focused service behavior-compatible by delegating cross-domain
operations to the owning :class:`SLMFeedbackService`. Pass 3 narrows these
whole-facade dependencies to explicit capabilities.
"""

from __future__ import annotations

from typing import Any,TYPE_CHECKING

if TYPE_CHECKING:
    from .service import SLMFeedbackService
    from ...core.cgh.localization import LocalizationResult


class FeedbackComponent:
    """Base for an extracted feedback application service."""

    def __init__(self,feedback: "SLMFeedbackService") -> None:
        self._feedback = feedback

    @property
    def _application_session(self):
        return self._feedback.application_session

    @property
    def _callbacks(self):
        return self._feedback._callbacks

    def _active_feedback_plane(self,section_key: str) -> str | None:
        return self._feedback._active_feedback_plane(section_key)

    def _require_editor_mode(self) -> None:
        self._feedback._require_editor_mode()

    def _section_changed(self,section_key: str) -> None:
        self._feedback._section_changed(section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._feedback._warning(title,message)

    def _error(self,title: str,error: Exception) -> None:
        self._feedback._error(title,error)

    def feedback_orientation(self,section_key: str):
        return self._feedback.feedback_orientation(section_key)

    def _update_committed_analysis(
        self,section_key: str,localization: "LocalizationResult | None"=None,
    ) -> None:
        self._feedback._update_committed_analysis(section_key,localization)

    def _apply_resolution_operation(self,*args,**kwargs):
        return self._feedback._apply_resolution_operation(*args,**kwargs)

    def _resolve_position_reference(self,section_key: str):
        return self._feedback._resolve_position_reference(section_key)

    def request_measurement(self,*args,**kwargs):
        return self._feedback.request_measurement(*args,**kwargs)

    def feedback_measurement_metadata(self,section_key: str):
        return self._feedback.feedback_measurement_metadata(section_key)

    def commit_measurement(self,*args,**kwargs):
        return self._feedback.commit_measurement(*args,**kwargs)

"""Qt-side automatic-feedback state presentation."""

from __future__ import annotations

from collections.abc import Callable,Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....application.feedback import AutomaticFeedbackState,SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


class AutomaticFeedbackCoordinator:
    """Synchronize automatic-feedback controls and section interaction locking."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        windows: Callable[[],Iterable[tuple[str,"CGHSessionWindow"]]],
        window_for: Callable[[str],"CGHSessionWindow | None"],
        flush_section: Callable[[str],None],
        set_interaction_locked: Callable[[bool],None],
        emit_operation_changed: Callable[[bool],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._windows = windows
        self._window_for = window_for
        self._flush_section = flush_section
        self._set_interaction_locked = set_interaction_locked
        self._emit_operation_changed = emit_operation_changed
        self._report_error = report_error

    @property
    def operation_active(self) -> bool:
        return self._feedback.automatic_operation_active

    def configure_availability(self,window: "CGHSessionWindow") -> None:
        window.set_automatic_feedback_available(
            self._feedback.can_run_automatic_feedback,
            self._feedback.automatic_feedback_unavailable_reason,
        )

    def apply_state_to_window(
        self,
        section_key: str,
        window: "CGHSessionWindow",
        *,
        state: "AutomaticFeedbackState | None"=None,
    ) -> None:
        state = self._feedback.automatic_state if state is None else state
        if not state.active:
            window.set_automatic_operation_state(False)
            return
        window.set_automatic_operation_state(
            True,
            owner=section_key == state.section_key,
            stopping=state.stop_requested,
            progress=state.progress,
        )

    def start(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool,
    ) -> None:
        try:
            self._flush_section(section_key)
            self._feedback.start_automatic_feedback(
                section_key,
                rounds=int(rounds),
                source=str(source or ""),
                reuse_previous_localization=bool(reuse_previous_localization),
            )
        except Exception as error:
            self._report_error("Automatic feedback failed",error)

    def stop(self) -> None:
        self._feedback.stop_automatic_feedback()

    def on_state_changed(self,state: "AutomaticFeedbackState") -> None:
        self._set_interaction_locked(bool(state.active))
        for section_key,window in tuple(self._windows()):
            self.apply_state_to_window(section_key,window,state=state)
        self._emit_operation_changed(bool(state.active))

    def on_finished(self,section_key: str,message: str) -> None:
        window = self._window_for(section_key)
        if window is not None:
            window.measurement_view.set_measurement_status(str(message))

    def refresh_availability(self) -> None:
        for section_key,window in tuple(self._windows()):
            self.configure_availability(window)
            self.apply_state_to_window(section_key,window)

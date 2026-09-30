"""Feedback adaptation, correction activation and round-history operations."""

from __future__ import annotations

from typing import Any,Callable,TYPE_CHECKING

from ...core.cgh.propagation import simulate_propagation_fft
from ...core.engine.transition import SectionStateTransition
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


class FeedbackAdaptationService(FeedbackComponent):
    """Apply resolved feedback operations to the authoritative runtime."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        cancel_cgh: Callable[[str],None],
        feedback_orientation: Callable[[str],Any],
        resolve_position_reference: Callable[[str],tuple[Any,dict[str,Any]]],
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._cancel_cgh = cancel_cgh
        self._feedback_orientation = feedback_orientation
        self._resolve_position_reference_callback = resolve_position_reference

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_intensity_feedback(section_key),
            "Applying intensity feedback failed",
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str) -> tuple[bool,SectionStateTransition | None]:
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.reset_section_intensity_feedback(section_key),
            "Resetting intensity feedback failed",
        )

    def apply_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        reference_positions,reference_metadata = self._resolve_position_reference_callback(
            section_key
        )
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
                orientation=self._feedback_orientation(section_key),
                reference_positions_px=reference_positions,
                reference_metadata=reference_metadata,
            ),
            "Applying position correction failed",
        )

    def set_position_active(
        self,section_key: str,active: bool,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.set_section_position_correction_active(
                section_key,bool(active),reset_intensity=bool(reset_intensity),
            ),
            "Changing position correction failed",
        )

    def clear_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.clear_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
            ),
            "Clearing position correction failed",
        )

    def _apply_resolution_operation(
        self,
        section_key: str,
        operation: Callable[["SLMRuntime"],SectionStateTransition | None],
        error_title: str,
        *,
        raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        if not self._editor_writes_allowed():
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        self._cancel_cgh(section_key)
        try:
            transition = operation(self._runtime)
            if transition is not None:
                self._callbacks.on_transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)
            return True,transition
        except Exception as error:
            self._section_changed(section_key)
            if raise_errors:
                raise
            self._error(error_title,error)
            return False,None

    def reset_to_round(self,section_key: str,round_index: int) -> SectionStateTransition | None:
        self._require_editor_mode()
        runtime = self._runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            raise ValueError("Only computed rounds can be restored.")
        self._cancel_cgh(section_key)
        transition = runtime.reset_section_cgh_to_round(section_key,int(round_index))
        if transition is not None:
            self._callbacks.on_transition_committed(section_key,transition)
        else:
            self._section_changed(section_key)
        return transition

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ):
        pad_size = int(pad_size)
        if pad_size <= 0:
            raise ValueError("CGH propagation pad size must be > 0")
        inspection = self._runtime.get_section_cgh_session_inspection(section_key)
        if str(position_context) == "not_corrected":
            selected = inspection.position_reference_round
        elif str(position_context) == "corrected":
            selected = next(
                (item for item in inspection.rounds if item.index == int(round_index)),
                None,
            )
        else:
            raise ValueError(
                "Unknown position history context: %r" % position_context
            )
        if selected is None or selected.result is None:
            raise RuntimeError("The selected round has no computed CGH result")
        return simulate_propagation_fft(
            selected.result.pattern,padding=True,pad_size=pad_size,
        )

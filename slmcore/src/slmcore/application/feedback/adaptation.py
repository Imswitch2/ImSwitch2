"""Feedback adaptation, correction activation and round-history operations."""

from __future__ import annotations

from typing import Any,TYPE_CHECKING

from ...core.cgh.propagation import simulate_propagation_fft
from ...core.engine.transition import SectionStateTransition
from .component import FeedbackComponent

if TYPE_CHECKING:
    from .service import SLMFeedbackService


class FeedbackAdaptationService(FeedbackComponent):
    """Apply resolved feedback operations to the authoritative runtime."""

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
        reference_positions,reference_metadata = self._resolve_position_reference(
            section_key
        )
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
                orientation=self.feedback_orientation(section_key),
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

    def reset_to_round(self,section_key: str,round_index: int) -> SectionStateTransition | None:
        self._require_editor_mode()
        runtime = self._application_session.runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            raise ValueError("Only computed rounds can be restored.")
        self._application_session.cancel_cgh(section_key)
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
        inspection = self._application_session.runtime.get_section_cgh_session_inspection(section_key)
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

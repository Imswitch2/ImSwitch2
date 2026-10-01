"""Qt-side feedback adaptation, round reset, and propagation workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any,TYPE_CHECKING

from qtpy import QtWidgets

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ....core.engine.runtime import SLMRuntime
    from ....core.engine.transition import SectionStateTransition
    from ...cgh.session_window import CGHSessionWindow


class FeedbackAdaptationCoordinator:
    """Present intensity/position adaptation operations and confirmations."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        runtime: Callable[[],"SLMRuntime"],
        flush_section: Callable[[str],None],
        synchronize_section: Callable[[str],None],
        editor_writes_allowed: Callable[[],bool],
        report_warning: Callable[[str,Any],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._runtime = runtime
        self._flush_section = flush_section
        self._synchronize_section = synchronize_section
        self._editor_writes_allowed = editor_writes_allowed
        self._report_warning = report_warning
        self._report_error = report_error

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._run_resolution(
            section_key,
            lambda:self._feedback.apply_intensity_feedback(
                section_key,raise_errors=raise_errors,
            ),
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str) -> None:
        self._run_resolution(
            section_key,
            lambda:self._feedback.reset_intensity_feedback(section_key),
        )

    def apply_position_correction(self,section_key: str) -> None:
        if not self._editor_writes_allowed():
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._run_resolution(
            section_key,
            lambda:self._feedback.apply_position_correction(
                section_key,reset_intensity=reset,
            ),
        )

    def set_position_active(
        self,section_key: str,active: bool,
    ) -> tuple[bool,SectionStateTransition | None]:
        if not self._editor_writes_allowed():
            return False,None
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return False,None
        return self._run_resolution(
            section_key,
            lambda:self._feedback.set_position_active(
                section_key,bool(active),reset_intensity=reset,
            ),
        )

    def clear_position_correction(
        self,section_key: str,
    ) -> tuple[bool,SectionStateTransition | None]:
        if not self._editor_writes_allowed():
            return False,None
        status = self._runtime().get_section_feedback_status(section_key)
        if bool(status.position_active):
            reset = self._confirm_position_intensity_reset(section_key)
            if reset is None:
                return False,None
        else:
            # Forgetting an inactive/stale correction does not change the
            # effective spot positions, so intensity feedback remains valid.
            reset = False
        return self._run_resolution(
            section_key,
            lambda:self._feedback.clear_position_correction(
                section_key,reset_intensity=reset,
            ),
        )

    def reset_to_round(self,section_key: str,round_index: int) -> None:
        if not self._editor_writes_allowed():
            return
        runtime = self._runtime()
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            self._report_warning(
                "Reset CGH round","Only computed rounds can be restored."
            )
            return
        later = [
            item.index for item in inspection.rounds
            if item.index > int(round_index)
        ]
        if not later and inspection.working_round is None:
            return
        if not self._confirm_round_reset(
            section_key,int(round_index),len(later),
            inspection.working_round is not None,
        ):
            return
        try:
            self._feedback.reset_to_round(section_key,int(round_index))
        except Exception as error:
            self._report_error("Reset CGH round failed",error)

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ) -> None:
        window = self._window_for(section_key)
        try:
            intensity = self._feedback.propagate_round(
                section_key,int(round_index),
                position_context=position_context,pad_size=pad_size,
            )
            if window is not None:
                window.set_propagation_result(int(round_index),intensity)
        except Exception as error:
            if window is not None:
                window.set_propagation_error(error)
            else:
                self._report_error("CGH propagation failed",error)

    def _run_resolution(
        self,section_key: str,
        operation: Callable[[],tuple[bool,SectionStateTransition | None]],
        *,raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        if not self._editor_writes_allowed():
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        try:
            self._flush_section(section_key)
            return operation()
        except Exception as error:
            if raise_errors:
                raise
            self._report_error("Feedback operation failed",error)
            self._synchronize_section(section_key)
            return False,None

    def _confirm_position_intensity_reset(self,section_key: str) -> bool | None:
        count = int(
            self._runtime().get_section_feedback_status(section_key).intensity_count
        )
        if count <= 0:
            return False
        parent = self._window_for(section_key)
        dialog = QtWidgets.QMessageBox(parent)
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setWindowTitle("Position correction")
        dialog.setText(
            "Changing spot positions resets the current CGH intensity "
            "sequence (%d feedback round(s))." % count
        )
        dialog.setInformativeText(
            "The previously applied hologram will remain displayed and be "
            "marked stale until a new Round 0 CGH is computed."
        )
        reset_button = dialog.addButton(
            "Reset intensity and apply",QtWidgets.QMessageBox.AcceptRole,
        )
        dialog.addButton(QtWidgets.QMessageBox.Cancel)
        dialog.setDefaultButton(reset_button)
        dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()
        return True if dialog.clickedButton() is reset_button else None

    def _confirm_round_reset(
        self,
        section_key: str,
        round_index: int,
        later_round_count: int,
        has_working_round: bool,
    ) -> bool:
        consequences = []
        if later_round_count:
            consequences.append("%d later computed round(s)" % later_round_count)
        if has_working_round:
            consequences.append("the current uncomputed working round")
        detail = " and ".join(consequences) or "later session state"
        parent = self._window_for(section_key)
        result = QtWidgets.QMessageBox.question(
            parent,
            "Reset CGH round",
            "Reset the session to Round %d? This will permanently discard %s."
            % (round_index,detail),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return result == QtWidgets.QMessageBox.Yes

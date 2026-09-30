"""Qt-side FOV position-calibration presentation and dialogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from qtpy import QtWidgets

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


class FOVPositionCalibrationCoordinator:
    """Synchronize and edit FOV position calibrations in feedback UI."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        flush_section: Callable[[str],None],
        synchronize_section: Callable[[str],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._flush_section = flush_section
        self._synchronize_section = synchronize_section
        self._report_error = report_error

    def synchronize(
        self,section_key: str,window: "CGHSessionWindow",
    ) -> None:
        window.set_fov_position_calibration_state(
            self._feedback.fov_position_calibration_context(section_key),
            self._feedback.fov_position_calibration_preview(section_key),
        )

    def fit(
        self,section_key: str,*,model: str="polynomial",degree: int=2,
    ) -> None:
        try:
            self._flush_section(section_key)
            self._feedback.fit_fov_position_calibration(
                section_key,model=model,degree=degree,
            )
            self._synchronize_section(section_key)
        except Exception as error:
            self._report_error("Fitting FOV calibration failed",error)
            self._synchronize_section(section_key)

    def save(self,section_key: str,name: str) -> None:
        if not name:
            return
        try:
            self._feedback.save_fov_position_calibration(section_key,name)
        except FileExistsError:
            parent = self._window_for(section_key)
            answer = QtWidgets.QMessageBox.question(
                parent,"Replace FOV calibration",
                'FOV calibration "%s" already exists for this SLM/section/plane. Replace it?'
                % name,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                self._feedback.save_fov_position_calibration(
                    section_key,name,overwrite=True,
                )
            except Exception as error:
                self._report_error("Saving FOV calibration failed",error)
        except Exception as error:
            self._report_error("Saving FOV calibration failed",error)
        self._synchronize_section(section_key)

    def replace_applied(self,section_key: str) -> None:
        parent = self._window_for(section_key)
        context = self._feedback.fov_position_calibration_context(section_key)
        name = context.get("selected_name")
        if not name:
            return
        warning = self._feedback.fov_position_calibration_replacement_warning(
            section_key
        )
        text = 'Replace applied FOV calibration "%s" with the current fit?' % name
        if warning:
            text += "\n\n" + warning
        answer = QtWidgets.QMessageBox.question(
            parent,"Replace applied FOV calibration",text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self._feedback.replace_applied_fov_position_calibration(section_key)
        except Exception as error:
            self._report_error("Replacing FOV calibration failed",error)
        self._synchronize_section(section_key)

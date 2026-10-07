"""Qt presentation for the saved/runtime FOV correction state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


class FOVPositionCalibrationCoordinator:
    """Synchronize read-only FOV correction inspection in the feedback UI."""

    def __init__(self,feedback_service: "SLMFeedbackService") -> None:
        self._feedback = feedback_service

    def synchronize(
        self,section_key: str,window: "CGHSessionWindow",
    ) -> None:
        window.set_fov_position_calibration_state(
            self._feedback.fov_position_calibration_context(section_key),
            self._feedback.fov_position_calibration_preview(section_key),
        )

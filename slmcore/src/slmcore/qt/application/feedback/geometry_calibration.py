"""Qt-side geometry-orientation calibration workflow."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any,Mapping,TYPE_CHECKING

import numpy as np
from PIL import Image
from qtpy import QtWidgets

from ....core.measurement import create_image_measurement

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


_logger = logging.getLogger(__name__)


class GeometryOrientationCalibrationCoordinator:
    """Drive geometry-orientation calibration UI without owning session state."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        flush_section: Callable[[str],None],
        synchronize_section: Callable[[str],None],
        report_error: Callable[[str,Exception],None],
        set_measurement_error: Callable[[str,Exception],None],
        set_localization_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._flush_section = flush_section
        self._synchronize_section = synchronize_section
        self._report_error = report_error
        self._set_measurement_error = set_measurement_error
        self._set_localization_error = set_localization_error

    def orientation_ui_context(self,section_key: str) -> dict[str,Any]:
        context = self._feedback.feedback_orientation_context(section_key)
        return {
            "observation_plane_name":context.observation_plane_name,
            "observation_plane_override":context.observation_plane_override,
            "saved_orientation":context.saved_orientation.value,
            "calibrated":context.calibrated,
            "change_enabled":context.change_allowed,
            "change_reason":context.change_unavailable_reason,
        }

    def synchronize(
        self,section_key: str,window: "CGHSessionWindow",
    ) -> None:
        window.set_geometry_orientation_calibration_state(
            self._feedback.geometry_orientation_calibration_context(section_key)
        )

    @staticmethod
    def _log_error(section_key: str,stage: str,error: Exception) -> None:
        _logger.error(
            "Geometry orientation calibration %s failed for section %s: %s",
            str(stage),str(section_key),error,
            exc_info=(type(error),error,error.__traceback__),
        )

    def _compute_complete(
        self,section_key: str,success: bool,error: Exception | None,
    ) -> None:
        window = self._window_for(section_key)
        self._synchronize_section(section_key)
        if success:
            if window is not None:
                window.measurement_view.set_measurement_status(
                    "Geometry calibration target is displayed on the SLM. "
                    "Adjust detector/exposure, then Acquire or Load and Localize."
                )
            return
        if error is not None:
            self._log_error(section_key,"target computation/upload",error)
            self._report_error("Geometry calibration failed",error)

    def _acquisition_complete(
        self,section_key: str,success: bool,error: Exception | None,
    ) -> None:
        window = self._window_for(section_key)
        self._synchronize_section(section_key)
        if success:
            if window is not None:
                window.measurement_view.set_measurement_status(
                    "Geometry calibration image acquired. Fine-tune localization "
                    "parameters and click Localize."
                )
            return
        if error is not None:
            self._log_error(section_key,"acquisition",error)
            if window is not None:
                window.measurement_view.set_measurement_status(
                    "Geometry calibration acquisition failed (%s). The calibration "
                    "target remains displayed; adjust acquisition settings and retry."
                    % error,
                    warning=True,
                )
            else:
                self._report_error("Geometry calibration acquisition failed",error)

    def compute(
        self,section_key: str,values: Mapping[str,Any],
    ) -> None:
        try:
            self._flush_section(section_key)
            px = float(values.get("period_x"))
            py = float(values.get("period_y"))
            if str(values.get("period_unit") or "") == "um":
                px = self._feedback.geometry_period_from_um(section_key,px,"x")
                py = self._feedback.geometry_period_from_um(section_key,py,"y")
            self._feedback.compute_geometry_orientation_calibration_target(
                section_key,
                grid_x=int(values.get("grid_x",5)),
                grid_y=int(values.get("grid_y",4)),
                period_x_px=px,
                period_y_px=py,
                source=str(values.get("detector") or ""),
                on_complete=(
                    lambda success,error,key=section_key:
                        self._compute_complete(key,success,error)
                ),
            )
        except Exception as error:
            self._log_error(section_key,"target computation setup",error)
            self._report_error("Geometry calibration failed",error)
            self._synchronize_section(section_key)

    def acquire(self,section_key: str,source: str) -> None:
        try:
            self._feedback.acquire_geometry_orientation_calibration(
                section_key,source,
                on_complete=(
                    lambda success,error,key=section_key:
                        self._acquisition_complete(key,success,error)
                ),
            )
        except Exception as error:
            self._log_error(section_key,"acquisition setup",error)
            self._set_measurement_error(section_key,error)

    def load(self,section_key: str) -> None:
        try:
            parent = self._window_for(section_key)
            path,_selected = QtWidgets.QFileDialog.getOpenFileName(
                parent,"Select geometry calibration image","",
                "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
            )
            if not path:
                return
            image = np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement = create_image_measurement(
                image,
                source="file",
                metadata={
                    "path":str(path),
                    "workflow":"geometry_orientation_calibration",
                },
            )
            self._feedback.set_geometry_orientation_calibration_measurement(
                section_key,measurement,
            )
            self._synchronize_section(section_key)
        except Exception as error:
            self._log_error(section_key,"image load",error)
            self._set_measurement_error(section_key,error)

    def run_localization(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        try:
            self._feedback.geometry_orientation_localization_candidate(
                section_key,parameters,
            )
            self._synchronize_section(section_key)
        except Exception as error:
            self._log_error(section_key,"localization",error)
            self._set_localization_error(section_key,error)

    def analyze(self,section_key: str) -> None:
        try:
            analysis = self._feedback.analyze_geometry_orientation_calibration(
                section_key
            )
            self._synchronize_section(section_key)
            if not analysis.accepted:
                window = self._window_for(section_key)
                if window is not None:
                    window.measurement_view.set_measurement_status(
                        "Geometry orientation is ambiguous; adjust/reacquire before saving.",
                        warning=True,
                    )
        except Exception as error:
            self._log_error(section_key,"analysis",error)
            self._report_error("Geometry calibration analysis failed",error)

    def save(self,section_key: str) -> None:
        try:
            self._feedback.save_geometry_orientation_calibration(section_key)
            self._synchronize_section(section_key)
        except Exception as error:
            self._log_error(section_key,"save",error)
            self._report_error("Saving geometry calibration failed",error)

    def finish(self,section_key: str) -> None:
        try:
            self._feedback.finish_geometry_orientation_calibration(section_key)
            self._synchronize_section(section_key)
            window = self._window_for(section_key)
            if window is not None:
                window.measurement_view.set_measurement_status(
                    "Geometry calibration finished. Current experiment CGH restored."
                )
        except Exception as error:
            self._log_error(section_key,"finish/restore",error)
            self._report_error("Finishing geometry calibration failed",error)

    def set_feedback_orientation(self,section_key: str,orientation: Any) -> None:
        self._feedback.set_feedback_orientation(section_key,orientation)

    def save_feedback_orientation(self,section_key: str) -> None:
        self._feedback.save_feedback_orientation(section_key)

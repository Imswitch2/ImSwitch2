"""Qt-side measurement acquisition and presentation for feedback workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any,Mapping,Sequence,TYPE_CHECKING

import numpy as np
from PIL import Image
from qtpy import QtWidgets

from ....core.measurement import ImageMeasurement,create_image_measurement

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


class MeasurementCoordinator:
    """Bridge measurement UI actions to the toolkit-neutral feedback facade."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        flush_section: Callable[[str],None],
        editor_writes_allowed: Callable[[],bool],
        report_error: Callable[[str,Exception],None],
        refresh_automatic_window: Callable[[str,"CGHSessionWindow"],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._flush_section = flush_section
        self._editor_writes_allowed = editor_writes_allowed
        self._report_error = report_error
        self._refresh_automatic_window = refresh_automatic_window

    def available_sources(self,section_key: str) -> Sequence[str]:
        return self._feedback.available_sources(section_key)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        return self._feedback.preferred_source(section_key,available)

    def select_observation_plane(
        self,section_key: str,observation_plane_name: str | None,
    ) -> bool:
        return self._feedback.select_observation_plane(section_key,observation_plane_name)

    def set_source_override(self,section_key: str,source: str | None) -> bool:
        return self._feedback.set_source_override(section_key,source)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> None:
        self._feedback.request_measurement(
            section_key,source,metadata=metadata,
            on_result=on_result,on_error=on_error,
        )

    def cancel_measurement(self,section_key: str) -> None:
        self._feedback.cancel_measurement(section_key)

    def acquire(
        self,section_key: str,source: str,*,reuse_previous_localization: bool,
    ) -> None:
        if not self._editor_writes_allowed():
            return
        try:
            self._flush_section(section_key)
            self._feedback.acquire(
                section_key,source,
                reuse_previous_localization=reuse_previous_localization,
            )
        except Exception as error:
            self.set_error(section_key,error)

    def capture_visual_background(self,section_key: str,source: str) -> None:
        """Capture a presentation-only image without committing feedback state."""
        if not self._editor_writes_allowed():
            return
        try:
            metadata=dict(self._feedback.feedback_measurement_metadata(section_key))
            metadata["purpose"]="position_visual_background"

            def on_result(measurement: ImageMeasurement) -> None:
                window=self._window_for(section_key)
                if window is not None:
                    window.set_position_visual_background(measurement)

            self._feedback.request_measurement(
                section_key,source,metadata=metadata,
                on_result=on_result,
                on_error=lambda error:self.set_error(section_key,error),
            )
        except Exception as error:
            self.set_error(section_key,error)

    def load(
        self,section_key: str,*,reuse_previous_localization: bool,
    ) -> None:
        if not self._editor_writes_allowed():
            return
        try:
            self._flush_section(section_key)
            parent = self._window_for(section_key)
            path,_selected = QtWidgets.QFileDialog.getOpenFileName(
                parent,"Select feedback image","",
                "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
            )
            if not path:
                return
            image = np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement = create_image_measurement(
                image,source="file",metadata={"path":str(path)},
            )
            self._feedback.commit_measurement(
                section_key,measurement,
                reuse_previous_localization=reuse_previous_localization,
            )
        except Exception as error:
            self.set_error(section_key,error)

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        return self._feedback.commit_measurement(
            section_key,measurement,
            reuse_previous_localization=reuse_previous_localization,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        return self._feedback.feedback_measurement_metadata(section_key)

    def on_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        window = self._window_for(section_key)
        if window is None:
            return
        window.set_measurement_busy(bool(busy),str(message or ""))
        self._refresh_automatic_window(section_key,window)

    def on_error(self,section_key: str,error: Exception) -> None:
        self.set_error(section_key,error)

    def set_error(self,section_key: str,error: Exception) -> None:
        window = self._window_for(section_key)
        if window is not None:
            window.set_measurement_error(error)
        else:
            self._report_error("Measurement failed",error)

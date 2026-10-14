"""Authoritative saved observation geometry consumed by feedback workflows."""

from __future__ import annotations

from typing import Callable,TYPE_CHECKING

from ...core.cgh.feedback import FeedbackOrientation,GeometryOrientationCalibration
from ...core.cgh.localization import LocalizationResult
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from ..observation_geometry import ObservationGeometryService
    from .contracts import SLMFeedbackCallbacks


class GeometryOrientationFeedbackService(FeedbackComponent):
    """Resolve feedback orientation exclusively from saved observation geometry."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        active_feedback_plane: Callable[[str],str | None],
        update_committed_analysis: Callable[[str,LocalizationResult | None],None],
        observation_geometry: "ObservationGeometryService | None"=None,
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._active_feedback_plane_provider = active_feedback_plane
        self._update_committed_analysis_callback = update_committed_analysis
        self._observation_geometry = observation_geometry
        if self._observation_geometry is not None:
            self._observation_geometry.add_listener(
                self._on_geometry_orientation_calibration_store_changed
            )

    def _active_feedback_plane(self,section_key: str) -> str | None:
        return self._active_feedback_plane_provider(section_key)

    def geometry_orientation_calibration(
        self,section_key: str,*,observation_plane_name: str | None=None,
    ) -> GeometryOrientationCalibration | None:
        """Load saved geometry for this section and active or given observation plane."""
        section=str(section_key)
        plane=(
            self._active_feedback_plane(section)
            if observation_plane_name is None
            else str(observation_plane_name or "").strip() or None
        )
        geometry=self._observation_geometry
        if plane is None or geometry is None:
            return None
        return geometry.calibration(section,plane)

    def feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        """Return saved geometry orientation, or identity when geometry is missing."""
        calibration=self.geometry_orientation_calibration(section_key)
        return (
            FeedbackOrientation.IDENTITY
            if calibration is None else calibration.orientation
        )

    def _on_geometry_orientation_calibration_store_changed(self) -> None:
        """Refresh committed feedback analysis when authoritative geometry changes."""
        for section_key in tuple(self._runtime.section_keys):
            section=str(section_key)
            if self._runtime.get_section_feedback_status(section).localization_available:
                self._update_committed_analysis_callback(section,None)
            self._feedback_state_changed(section)

    def dispose(self) -> None:
        if self._observation_geometry is not None:
            self._observation_geometry.remove_listener(
                self._on_geometry_orientation_calibration_store_changed
            )

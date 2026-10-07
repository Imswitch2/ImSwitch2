"""Feedback orientation state and contextual geometry-calibration frontend."""

from __future__ import annotations

from typing import Callable,Mapping,TYPE_CHECKING

from ...core.cgh.feedback import (
    FeedbackOrientation,GeometryOrientationCalibration,GeometryOrientationAnalysis,
)
from ...core.cgh.localization import LocalizationResult
from ...core.measurement import ImageMeasurement
from ..geometry_calibration import GeometryCalibrationContext
from .component import FeedbackComponent
from .contracts import FeedbackOrientationContext

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from ..observation_geometry import ObservationGeometryService
    from ..geometry_calibration import GeometryCalibrationService
    from ..startup_preferences import StartupPreferencesState
    from .contracts import SLMFeedbackCallbacks



class GeometryOrientationFeedbackService(FeedbackComponent):
    """Own feedback orientation state; delegate geometry calibration to the shared service."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        active_feedback_plane: Callable[[str],str | None],
        startup_preferences: Callable[[], "StartupPreferencesState | None"],
        update_committed_analysis: Callable[[str,LocalizationResult | None],None],
        observation_geometry: "ObservationGeometryService | None"=None,
        geometry_calibration: "GeometryCalibrationService",
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._active_feedback_plane_provider = active_feedback_plane
        self._startup_preferences_provider = startup_preferences
        self._update_committed_analysis_callback = update_committed_analysis
        self._observation_geometry = observation_geometry
        self._geometry_calibration = geometry_calibration
        self._feedback_orientations: dict[
            str,tuple[str | None,FeedbackOrientation]
        ] = {}
        if self._observation_geometry is not None:
            self._observation_geometry.add_listener(
                self._on_geometry_orientation_calibration_store_changed
            )

    def _active_feedback_plane(self,section_key: str) -> str | None:
        return self._active_feedback_plane_provider(section_key)

    def _saved_feedback_orientation(
        self,section_key: str,observation_plane_name: str | None,
    ) -> FeedbackOrientation:
        calibration = self.geometry_orientation_calibration(
            section_key,observation_plane_name=observation_plane_name,
        )
        if calibration is not None:
            return calibration.orientation
        preferences = self._startup_preferences_provider()
        value = (
            "identity" if preferences is None
            else preferences.feedback_orientation(section_key,observation_plane_name)
        )
        return FeedbackOrientation.normalize(value)

    def geometry_orientation_calibration(
        self,section_key: str,*,observation_plane_name: str | None=None,
    ) -> GeometryOrientationCalibration | None:
        """Load saved geometry for this SLM/section and active or given observation plane."""
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

    def geometry_period_from_um(self,section_key: str,value_um: float,axis: str) -> float:
        return self._geometry_calibration.period_from_um(section_key,value_um,axis)

    def geometry_orientation_calibration_context(
        self,section_key: str,
    ) -> GeometryCalibrationContext:
        return self._geometry_calibration.context(
            section_key,self._active_feedback_plane(section_key),
        )

    def _feedback_orientation_change_reason(self,section_key: str) -> str:
        status = self._runtime.get_section_cgh_status(section_key)
        current = status.current_round_index
        feedback_cgh_computed = bool(
            current is not None
            and (int(current) > 0 or bool(status.position_active))
        )
        if feedback_cgh_computed:
            return (
                "Feedback orientation is locked after a feedback-corrected "
                "hologram has been computed. Reset the intensity feedback to "
                "the base round or clear the position correction before "
                "changing orientation."
            )
        return ""

    def feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        current = self._feedback_orientations.get(section)
        if current is None or current[0] != plane:
            orientation = self._saved_feedback_orientation(section,plane)
            self._feedback_orientations[section] = (plane,orientation)
            return orientation
        return current[1]

    def feedback_orientation_context(
        self,section_key: str,
    ) -> FeedbackOrientationContext:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        orientation = self.feedback_orientation(section)
        saved = self._saved_feedback_orientation(section,plane)
        preferences = self._startup_preferences_provider()
        observation_plane_override = bool(
            plane
            and preferences is not None
            and preferences.feedback_orientation_for_observation_plane(section,plane) is not None
        )
        reason = self._feedback_orientation_change_reason(section)
        return FeedbackOrientationContext(
            orientation=orientation,
            saved_orientation=saved,
            observation_plane_name=plane,
            observation_plane_override=observation_plane_override,
            calibrated=self.geometry_orientation_calibration(section,observation_plane_name=plane) is not None,
            change_allowed=not bool(reason),
            change_unavailable_reason=reason,
        )

    def _require_active_geometry_plane(self,section_key: str) -> str:
        plane=self._active_feedback_plane(section_key)
        if plane is None:
            raise RuntimeError("Select an observation plane before geometry calibration.")
        return plane

    def compute_geometry_orientation_calibration_target(
        self,section_key: str,*,grid_x: int,grid_y: int,period_x_px: float,period_y_px: float,
        source: str,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        plane=self._require_active_geometry_plane(section_key)
        return self._geometry_calibration.compute_target(
            section_key,plane,grid_x=grid_x,grid_y=grid_y,
            period_x_px=period_x_px,period_y_px=period_y_px,source=source,
            on_complete=on_complete,
        )

    def acquire_geometry_orientation_calibration(
        self,section_key: str,source: str,*,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> None:
        plane=self._require_active_geometry_plane(section_key)
        return self._geometry_calibration.acquire(
            section_key,plane,source,on_complete=on_complete,
        )

    def set_geometry_orientation_calibration_measurement(
        self,section_key: str,measurement: ImageMeasurement,
    ) -> None:
        plane=self._require_active_geometry_plane(section_key)
        return self._geometry_calibration.set_measurement(section_key,plane,measurement)

    def geometry_orientation_localization_candidate(
        self,section_key: str,parameters: Mapping[str,object],
    ) -> LocalizationResult:
        plane=self._require_active_geometry_plane(section_key)
        return self._geometry_calibration.localize(section_key,plane,parameters)

    def analyze_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationAnalysis:
        plane=self._require_active_geometry_plane(section_key)
        return self._geometry_calibration.analyze(section_key,plane)

    def save_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationCalibration:
        section=str(section_key)
        plane=self._require_active_geometry_plane(section)
        calibration=self._geometry_calibration.save(section,plane)
        self._feedback_orientations[section]=(plane,calibration.orientation)
        if self._runtime.get_section_feedback_status(section).localization_available:
            self._update_committed_analysis_callback(section,None)
        self._section_changed(section)
        return calibration

    def finish_geometry_orientation_calibration(self,section_key: str) -> None:
        return self._geometry_calibration.finish(section_key)

    def _on_geometry_orientation_calibration_store_changed(self) -> None:
        self.refresh_feedback_orientation_contexts(force=True)

    def set_feedback_orientation(
        self,section_key: str,orientation: FeedbackOrientation | str,
    ) -> FeedbackOrientation:
        """Set this plane's transient orientation and recompute existing intensity metrics."""
        self._require_editor_mode()
        section = str(section_key)
        reason = self._feedback_orientation_change_reason(section)
        if reason:
            raise RuntimeError(reason)
        normalized = FeedbackOrientation.normalize(orientation)
        plane = self._active_feedback_plane(section)
        self._feedback_orientations[section] = (plane,normalized)
        if self._runtime.get_section_feedback_status(section).localization_available:
            self._update_committed_analysis_callback(section,None)
        self._section_changed(section)
        return normalized

    def save_feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        """Persist the current transient orientation for the active observation plane/default."""
        self._require_editor_mode()
        section = str(section_key)
        orientation = self.feedback_orientation(section)
        plane = self._active_feedback_plane(section)
        if plane is None:
            preferences = self._startup_preferences_provider()
            if preferences is None:
                raise RuntimeError("Startup preferences are unavailable")
            preferences.set_feedback_orientation_default(section,orientation.value)
        elif self._observation_geometry is None or self._observation_geometry.store is None:
            # Preserve the legacy no-workspace fallback without duplicating the
            # actual geometry-calibration workflow.
            preferences = self._startup_preferences_provider()
            if preferences is None:
                raise RuntimeError("Geometry orientation-calibration storage is unavailable.")
            preferences.set_feedback_orientation_for_observation_plane(section,plane,orientation.value)
        else:
            self._geometry_calibration.save_manual(section,plane,orientation)
            self._feedback_orientations[section] = (plane,orientation)
        self._section_changed(section)
        return orientation

    def refresh_feedback_orientation_contexts(self,*,force: bool=False) -> None:
        """Refresh orientation after plane changes or authoritative calibration changes."""
        for section_key in tuple(self._runtime.section_keys):
            section = str(section_key)
            current = self._feedback_orientations.get(section)
            if current is None:
                continue
            plane = self._active_feedback_plane(section)
            if current[0] == plane and not force:
                continue
            orientation = self._saved_feedback_orientation(section,plane)
            self._feedback_orientations[section] = (plane,orientation)
            if self._runtime.get_section_feedback_status(section).localization_available:
                self._update_committed_analysis_callback(section,None)
            self._section_changed(section)

    def prepare_runtime_change(self) -> None:
        self._feedback_orientations.clear()

    def dispose(self) -> None:
        if self._observation_geometry is not None:
            self._observation_geometry.remove_listener(
                self._on_geometry_orientation_calibration_store_changed
            )
        self._feedback_orientations.clear()

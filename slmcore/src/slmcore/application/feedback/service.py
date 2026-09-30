"""Public toolkit-independent feedback facade.

The facade preserves the historical ``session.feedback.*`` API while focused
application services own the corresponding workflow state and implementation.
Cross-service dependencies still route through this facade in Pass 2; Pass 3
will replace that temporary bridge with narrow explicit dependencies.
"""

from __future__ import annotations

from typing import Any,Callable,Mapping,Sequence,TYPE_CHECKING

import numpy as np

from ...core.cgh.feedback import (
    EditablePositionReferenceGeometry,FeedbackOrientation,FOVPositionCalibration,
    GeometryOrientationAnalysis,GeometryOrientationCalibration,IntensityAnalysis,
    PositionReference,PositionReferenceMode,
)
from ...core.cgh.localization import LocalizationResult
from ...core.measurement import ImageMeasurement
from ...core.engine.transition import SectionStateTransition
from ..position_reference_store import (
    PositionReferenceLifetime,PositionReferenceRecord,PositionReferenceStore,
)
from .adaptation import FeedbackAdaptationService
from .automatic import AutomaticFeedbackRunner
from .contracts import (
    AutomaticFeedbackState,FeedbackOrientationContext,FeedbackParameterUpdateResult,
    MeasurementDispatcher,SLMFeedbackCallbacks,
)
from .fov_calibration import (
    FOVPositionCalibrationContext,FOVPositionCalibrationPreview,
    FOVPositionCalibrationService,
)
from .geometry_calibration import (
    GeometryCalibrationPayload,GeometryOrientationCalibrationContext,
    GeometryOrientationFeedbackService,_GeometryCalibrationWorkflow,
)
from .localization import LocalizationService
from .measurement import MeasurementService
from .references import (
    PositionReferenceContext,PositionReferencePreview,PositionReferenceResolutionMetadata,
    PositionReferenceSelection,PositionReferenceService,
)

if TYPE_CHECKING:
    from ..session import SLMSession
    from ...workspace.fov_position_calibration_store import SLMFOVPositionCalibrationStore
    from ...workspace.geometry_orientation_calibration_store import (
        SLMGeometryOrientationCalibrationStore,
    )


class SLMFeedbackService:
    """Compose focused feedback workflows for one toolkit-neutral SLM session."""

    def __init__(
        self,
        application_session: "SLMSession",
        *,
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMFeedbackCallbacks | None=None,
        position_reference_store: PositionReferenceStore | None=None,
        fov_position_calibration_store: "SLMFOVPositionCalibrationStore | None"=None,
        geometry_orientation_calibration_store: (
            "SLMGeometryOrientationCalibrationStore | None"
        )=None,
    ) -> None:
        self._application_session = application_session
        self._callbacks = callbacks or SLMFeedbackCallbacks()
        self._measurement = MeasurementService(self,measurements)
        self._geometry = GeometryOrientationFeedbackService(
            self,geometry_orientation_calibration_store,
        )
        self._references = PositionReferenceService(self,position_reference_store)
        self._fov_calibration = FOVPositionCalibrationService(
            self,fov_position_calibration_store,
        )
        self._localization = LocalizationService(self)
        self._adaptation = FeedbackAdaptationService(self)
        self._automatic = AutomaticFeedbackRunner(self)

    @property
    def application_session(self) -> "SLMSession":
        """Toolkit-neutral session that owns this feedback facade."""
        return self._application_session

    def set_callbacks(self,callbacks: SLMFeedbackCallbacks | None) -> None:
        self._callbacks = callbacks or SLMFeedbackCallbacks()

    def set_measurement_dispatcher(
        self,measurements: MeasurementDispatcher | None,
    ) -> None:
        if measurements is self._measurement.dispatcher:
            return
        self.prepare_runtime_change()
        self._measurement.set_dispatcher(measurements)

    @property
    def automatic_operation_active(self) -> bool:
        return self._automatic.active

    @property
    def automatic_state(self) -> AutomaticFeedbackState:
        return self._automatic.state

    @property
    def can_run_automatic_feedback(self) -> bool:
        measurements = self._measurement.dispatcher
        return bool(
            self._application_session.editor_writes_allowed
            and self._application_session.auto_upload_frame
            and self._application_session.host_services.can_upload_frame
            and measurements is not None
            and measurements.available
            and not self._application_session.upload_deferred
        )

    @property
    def automatic_feedback_unavailable_reason(self) -> str:
        if not self._application_session.editor_writes_allowed:
            return "Automatic feedback is unavailable in Fast Config mode."
        if not self._application_session.auto_upload_frame:
            return "Automatic feedback is disabled when auto_upload_frame=False."
        if not self._application_session.host_services.can_upload_frame:
            return "Automatic feedback requires a frame-upload capability."
        measurements = self._measurement.dispatcher
        if measurements is None or not measurements.available:
            return "Automatic feedback requires a measurement provider."
        if self._application_session.upload_deferred:
            return "Automatic feedback is unavailable while frame upload is deferred."
        return ""

    def _active_feedback_plane(self,section_key: str) -> str | None:
        calibration = getattr(self._application_session,"calibration",None)
        if calibration is None:
            return None
        plane = calibration.active_plane(section_key)
        return str(plane or "").strip() or None


    # ------------------------------------------------------------------
    # Measurement acquisition
    # ------------------------------------------------------------------

    def available_sources(self,section_key: str) -> Sequence[str]:
        return self._measurement.available_sources(section_key)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        return self._measurement.preferred_source(section_key,available)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> None:
        return self._measurement.request_measurement(
            section_key,
            source,
            metadata=metadata,
            on_result=on_result,
            on_error=on_error,
        )

    def cancel_measurement(self,section_key: str) -> None:
        return self._measurement.cancel_measurement(section_key)

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        reuse_previous_localization: bool=False,
    ) -> None:
        return self._measurement.acquire(
            section_key,
            source,
            reuse_previous_localization=reuse_previous_localization,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        return self._measurement.feedback_measurement_metadata(section_key)

    # ------------------------------------------------------------------
    # Position references
    # ------------------------------------------------------------------

    def _position_reference_key(
        self,section_key: str,
    ) -> tuple[str,str | None]:
        return self._references._position_reference_key(section_key)

    def _position_reference_change_reason(self,section_key: str) -> str:
        return self._references._position_reference_change_reason(section_key)

    def position_reference_selection(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        return self._references.position_reference_selection(section_key)

    def position_reference_context(
        self,section_key: str,
    ) -> PositionReferenceContext:
        return self._references.position_reference_context(section_key)

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: PositionReferenceMode | str,
        reference_name: str | None=None,
        editable_geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | Sequence[str] | None=None,
    ) -> PositionReferenceSelection:
        return self._references.set_position_reference(
            section_key,
            mode=mode,
            reference_name=reference_name,
            editable_geometry=editable_geometry,
            editable_locks=editable_locks,
        )

    def fit_position_reference_center(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        return self._references.fit_position_reference_center(section_key)

    def add_position_reference(
        self,
        section_key: str,
        name: str,
        *,
        lifetime: PositionReferenceLifetime | str=PositionReferenceLifetime.SESSION,
        overwrite: bool=False,
    ) -> PositionReference:
        return self._references.add_position_reference(
            section_key,name,lifetime=lifetime,overwrite=overwrite,
        )

    def persist_position_reference(
        self,section_key: str,name: str,
    ) -> PositionReferenceRecord:
        return self._references.persist_position_reference(section_key,name)

    def delete_position_reference(self,section_key: str,name: str) -> None:
        return self._references.delete_position_reference(section_key,name)

    def position_reference_preview(
        self,section_key: str,
    ) -> PositionReferencePreview | None:
        return self._references.position_reference_preview(section_key)

    def _resolve_position_reference(
        self,section_key: str,
    ) -> tuple[np.ndarray | None,PositionReferenceResolutionMetadata]:
        return self._references._resolve_position_reference(section_key)

    def _position_reference_create_reason(self,section_key: str) -> str:
        return self._references._position_reference_create_reason(section_key)

    def _on_position_reference_store_changed(self) -> None:
        return self._references._on_position_reference_store_changed()

    # ------------------------------------------------------------------
    # FOV position calibration
    # ------------------------------------------------------------------

    def _fov_position_key(self,section_key: str) -> tuple[str,str | None]:
        return self._fov_calibration._fov_position_key(section_key)

    def _saved_fov_position_calibration_name(
        self,section_key: str,plane_name: str | None,
    ) -> str | None:
        return self._fov_calibration._saved_fov_position_calibration_name(
            section_key,
            plane_name,
        )

    def _load_fov_position_calibration(
        self,section_key: str,plane_name: str,name: str,
    ) -> FOVPositionCalibration:
        return self._fov_calibration._load_fov_position_calibration(section_key,plane_name,name)

    def fov_position_calibration_context(self,section_key: str) -> FOVPositionCalibrationContext:
        return self._fov_calibration.fov_position_calibration_context(section_key)

    def fov_position_calibration_preview(self,section_key: str) -> FOVPositionCalibrationPreview | None:
        return self._fov_calibration.fov_position_calibration_preview(section_key)

    def select_fov_position_calibration(
        self,section_key: str,name: str | None,
    ) -> None:
        return self._fov_calibration.select_fov_position_calibration(section_key,name)

    def set_fov_position_calibration_applied(
        self,section_key: str,applied: bool,
    ) -> None:
        return self._fov_calibration.set_fov_position_calibration_applied(section_key,applied)

    def set_default_fov_position_calibration(self,section_key: str) -> None:
        return self._fov_calibration.set_default_fov_position_calibration(section_key)

    def delete_fov_position_calibration(self,section_key: str) -> str:
        return self._fov_calibration.delete_fov_position_calibration(section_key)

    def fit_fov_position_calibration(
        self,section_key: str,*,model: str="polynomial",degree: int=2,
    ) -> FOVPositionCalibration:
        return self._fov_calibration.fit_fov_position_calibration(
            section_key,
            model=model,
            degree=degree,
        )

    def save_fov_position_calibration(
        self,section_key: str,name: str,*,overwrite: bool=False,
    ) -> FOVPositionCalibration:
        return self._fov_calibration.save_fov_position_calibration(
            section_key,
            name,
            overwrite=overwrite,
        )

    def fov_position_calibration_replacement_warning(
        self,section_key: str,
    ) -> str:
        return self._fov_calibration.fov_position_calibration_replacement_warning(section_key)

    def replace_applied_fov_position_calibration(self,section_key: str) -> str:
        return self._fov_calibration.replace_applied_fov_position_calibration(section_key)

    def _set_runtime_fov_position_calibration(
        self,section_key: str,calibration: FOVPositionCalibration | None,
        *,notify: bool=True,
    ) -> None:
        # Context refreshes happen for all sections on plane/catalog changes.
        # Do not cancel a CGH request or create redundant callbacks when this
        # section already holds the exact same persistent field.
        return self._fov_calibration._set_runtime_fov_position_calibration(
            section_key,
            calibration,
            notify=notify,
        )

    def _warn_fov_extrapolation(self,section_key: str) -> None:
        return self._fov_calibration._warn_fov_extrapolation(section_key)

    def apply_startup_fov_position_calibration_defaults(self) -> None:
        return self._fov_calibration.apply_startup_fov_position_calibration_defaults()

    def refresh_fov_position_calibration_contexts(self) -> None:
        return self._fov_calibration.refresh_fov_position_calibration_contexts()

    def _on_fov_position_calibration_store_changed(self) -> None:
        return self._fov_calibration._on_fov_position_calibration_store_changed()

    # ------------------------------------------------------------------
    # Feedback orientation and geometry calibration
    # ------------------------------------------------------------------

    def _saved_feedback_orientation(
        self,section_key: str,plane_name: str | None,
    ) -> FeedbackOrientation:
        return self._geometry._saved_feedback_orientation(section_key,plane_name)

    def geometry_orientation_calibration(
        self,section_key: str,*,plane_name: str | None=None,
    ) -> GeometryOrientationCalibration | None:
        return self._geometry.geometry_orientation_calibration(
            section_key,
            plane_name=plane_name,
        )

    def _geometry_calibration_key(self,section_key: str) -> tuple[str,str | None]:
        return self._geometry._geometry_calibration_key(section_key)

    def _active_geometry_calibration_frame(
        self,section_key: str,
    ) -> tuple[str | None,_GeometryCalibrationWorkflow] | None:
        return self._geometry._active_geometry_calibration_frame(section_key)

    def _geometry_period_guess_px(
        self,section_key: str,grid_x: int,grid_y: int,
    ) -> tuple[float,float]:
        return self._geometry._geometry_period_guess_px(section_key,grid_x,grid_y)

    def _geometry_period_to_um(self,section_key: str,value: float,axis: str) -> float | None:
        return self._geometry._geometry_period_to_um(section_key,value,axis)

    def geometry_period_from_um(self,section_key: str,value_um: float,axis: str) -> float:
        return self._geometry.geometry_period_from_um(section_key,value_um,axis)

    def geometry_orientation_calibration_context(self,section_key: str) -> GeometryOrientationCalibrationContext:
        return self._geometry.geometry_orientation_calibration_context(section_key)

    def _feedback_orientation_change_reason(self,section_key: str) -> str:
        return self._geometry._feedback_orientation_change_reason(section_key)

    def feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        return self._geometry.feedback_orientation(section_key)

    def feedback_orientation_context(
        self,section_key: str,
    ) -> FeedbackOrientationContext:
        return self._geometry.feedback_orientation_context(section_key)

    def compute_geometry_orientation_calibration_target(
        self,section_key: str,*,grid_x: int,grid_y: int,period_x_px: float,period_y_px: float,
        source: str,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        return self._geometry.compute_geometry_orientation_calibration_target(
            section_key,
            grid_x=grid_x,
            grid_y=grid_y,
            period_x_px=period_x_px,
            period_y_px=period_y_px,
            source=source,
            on_complete=on_complete,
        )

    def acquire_geometry_orientation_calibration(
        self,section_key: str,source: str,*,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> None:
        return self._geometry.acquire_geometry_orientation_calibration(
            section_key,
            source,
            on_complete=on_complete,
        )

    def set_geometry_orientation_calibration_measurement(
        self,section_key: str,measurement: ImageMeasurement,
    ) -> None:
        return self._geometry.set_geometry_orientation_calibration_measurement(
            section_key,
            measurement,
        )

    def _localize_geometry_orientation_workflow(
        self,section_key: str,workflow: _GeometryCalibrationWorkflow,parameters: Mapping[str,Any] | None,
    ) -> LocalizationResult:
        return self._geometry._localize_geometry_orientation_workflow(
            section_key,
            workflow,
            parameters,
        )

    def geometry_orientation_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> LocalizationResult:
        return self._geometry.geometry_orientation_localization_candidate(
            section_key,
            parameters,
        )

    def analyze_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationAnalysis:
        return self._geometry.analyze_geometry_orientation_calibration(section_key)

    def save_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationCalibration:
        return self._geometry.save_geometry_orientation_calibration(section_key)

    def finish_geometry_orientation_calibration(self,section_key: str) -> None:
        return self._geometry.finish_geometry_orientation_calibration(section_key)

    def _on_geometry_orientation_calibration_store_changed(self) -> None:
        return self._geometry._on_geometry_orientation_calibration_store_changed()

    def set_feedback_orientation(
        self,section_key: str,orientation: FeedbackOrientation | str,
    ) -> FeedbackOrientation:
        return self._geometry.set_feedback_orientation(section_key,orientation)

    def save_feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        return self._geometry.save_feedback_orientation(section_key)

    def refresh_feedback_orientation_contexts(self,*,force: bool=False) -> None:
        return self._geometry.refresh_feedback_orientation_contexts(force=force)

    # ------------------------------------------------------------------
    # Localization
    # ------------------------------------------------------------------

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        return self._localization.commit_measurement(
            section_key,
            measurement,
            reuse_previous_localization=reuse_previous_localization,
        )

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        return self._localization.localization_candidate(section_key,parameters)

    def infer_missing_localization_candidate(
        self,section_key: str,localization: LocalizationResult,
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        return self._localization.infer_missing_localization_candidate(section_key,localization)

    def accept_localization(
        self,
        section_key: str,
        localization: LocalizationResult,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> bool:
        return self._localization.accept_localization(
            section_key,
            localization,
            parameters,
            raise_errors=raise_errors,
        )

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> LocalizationResult | None:
        return self._localization.reuse_localization(section_key,raise_errors=raise_errors)

    def localize_and_commit(self,section_key: str) -> None:
        return self._localization.localize_and_commit(section_key)

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: LocalizationResult | None=None,
    ) -> FeedbackParameterUpdateResult:
        return self._localization.update_parameters(
            section_key,
            group,
            changes,
            localization=localization,
        )

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        return self._localization.localization_context(section_key)

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        return self._localization.require_current_cgh_for_localization_commit(section_key)

    def validate_target_hint_use(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        return self._localization.validate_target_hint_use(section_key,parameters)

    # ------------------------------------------------------------------
    # Feedback adaptation and round history
    # ------------------------------------------------------------------

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.apply_intensity_feedback(section_key,raise_errors=raise_errors)

    def reset_intensity_feedback(self,section_key: str) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.reset_intensity_feedback(section_key)

    def apply_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.apply_position_correction(
            section_key,
            reset_intensity=reset_intensity,
        )

    def set_position_active(
        self,section_key: str,active: bool,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.set_position_active(
            section_key,
            active,
            reset_intensity=reset_intensity,
        )

    def clear_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.clear_position_correction(
            section_key,
            reset_intensity=reset_intensity,
        )

    def reset_to_round(self,section_key: str,round_index: int) -> SectionStateTransition | None:
        return self._adaptation.reset_to_round(section_key,round_index)

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ):
        return self._adaptation.propagate_round(
            section_key,
            round_index,
            position_context=position_context,
            pad_size=pad_size,
        )
    # ------------------------------------------------------------------
    # Automatic feedback and facade lifecycle
    # ------------------------------------------------------------------

    def start_automatic_feedback(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        return self._automatic.start(
            section_key,
            rounds=rounds,
            source=source,
            reuse_previous_localization=reuse_previous_localization,
        )

    def stop_automatic_feedback(self) -> None:
        self._automatic.stop()

    def prepare_runtime_change(self) -> None:
        self._automatic.cancel_for_runtime_change()
        self._measurement.cancel_all()
        self._geometry.prepare_runtime_change()

    def dispose(self) -> None:
        self.prepare_runtime_change()
        self._references.dispose()
        self._fov_calibration.dispose()
        self._geometry.dispose()

    # ------------------------------------------------------------------
    # Shared cross-workflow operations
    # ------------------------------------------------------------------

    def _update_committed_analysis(
        self,section_key: str,localization: LocalizationResult | None=None,
    ) -> None:
        try:
            runtime = self._application_session.runtime
            analysis = runtime.compute_section_feedback_intensity_analysis(
                section_key,localization,
                orientation=self.feedback_orientation(section_key),
            )
            runtime.set_section_feedback_intensity_analysis(section_key,analysis)
        except Exception as error:
            self._warning(
                "Intensity analysis",
                "Measurement metrics are unavailable: %s" % error,
            )

    def _apply_resolution_operation(
        self,
        section_key: str,
        operation: Callable[[Any],SectionStateTransition | None],
        error_title: str,
        *,
        raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        if not self._application_session.editor_writes_allowed:
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        self._application_session.cancel_cgh(section_key)
        try:
            transition = operation(self._application_session.runtime)
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

    def _section_changed(self,section_key: str) -> None:
        self._callbacks.on_section_changed(section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._callbacks.on_warning(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self._callbacks.on_error(str(title),error)

    def _require_editor_mode(self) -> None:
        if not self._application_session.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    @property
    def _fov_position_calibration_candidates(self):
        """Compatibility view of transient FOV candidates; not a public API."""
        return self._fov_calibration._fov_position_calibration_candidates

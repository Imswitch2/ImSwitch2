"""Public toolkit-independent feedback facade over focused feedback services."""

from __future__ import annotations

from dataclasses import replace
from typing import Any,Callable,Mapping,Sequence,TYPE_CHECKING

import numpy as np

from ...core.cgh.feedback import (
    EditablePositionReferenceGeometry,FeedbackOrientation,
    GeometryOrientationCalibration,IntensityAnalysis,PositionReference,
    PositionReferenceMode,
)
from ...core.cgh.localization import LocalizationResult
from ...core.measurement import ImageMeasurement
from ...core.engine.transition import SectionStateTransition
from ...workspace.position_reference_store import (
    PositionReferenceLifetime,PositionReferenceRecord,PositionReferenceStore,
)
from .adaptation import FeedbackAdaptationService
from .automatic import AutomaticFeedbackRunner
from .contracts import (
    AutomaticFeedbackState,FeedbackParameterUpdateResult,MeasurementDispatcher,
    SLMFeedbackCallbacks,
)
from .geometry_calibration import GeometryOrientationFeedbackService
from .localization import LocalizationService
from .measurement import MeasurementService
from .references import (
    PositionReferenceContext,PositionReferencePreview,PositionReferenceResolutionMetadata,
    PositionReferenceSelection,PositionReferenceService,
)

if TYPE_CHECKING:
    from ..session import SLMSession
    from ..observation_geometry import ObservationGeometryService
    from ..observation_registration import ObservationRegistrationService


_UNSET = object()


class SLMFeedbackService:
    """Compose focused feedback workflows for one toolkit-neutral SLM session."""

    def __init__(
        self,
        application_session: "SLMSession",
        *,
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMFeedbackCallbacks | None=None,
        position_reference_store: PositionReferenceStore | None=None,
        observation_geometry: "ObservationGeometryService | None"=None,
        observation_registration: "ObservationRegistrationService | None"=None,
    ) -> None:
        self._application_session = application_session
        self._callbacks = callbacks or SLMFeedbackCallbacks()
        # Feedback owns observation selection independently from mapping/calibration.
        self._observation_plane_by_section: dict[str,str | None] = {}
        self._source_override_by_section: dict[str,str] = {}

        runtime_provider = lambda:self._application_session.runtime
        callbacks_provider = lambda:self._callbacks
        editor_writes_allowed = lambda:bool(self._application_session.editor_writes_allowed)
        self._measurement = MeasurementService(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
            commit_measurement=lambda *args,**kwargs:
                self._localization.commit_measurement(*args,**kwargs),
            dispatcher=measurements,
            measurement_context=self._measurement_context,
            validate_measurement_context=self._require_observation_plane_for_measurement,
        )
        self._geometry = GeometryOrientationFeedbackService(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
            active_feedback_plane=self._active_feedback_plane,
            update_committed_analysis=self._update_committed_analysis,
            observation_geometry=observation_geometry,
        )
        self._references = PositionReferenceService(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
            active_feedback_plane=self._active_feedback_plane,
            feedback_orientation=self._geometry.feedback_orientation,
            current_config_path=lambda:self._application_session.current_config_path,
            store=position_reference_store,
            observation_registration=observation_registration,
        )
        self._localization = LocalizationService(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
            feedback_orientation=self._geometry.feedback_orientation,
            update_committed_analysis=self._update_committed_analysis,
            refresh_editable_reference=(
                self._references.refresh_editable_geometry_after_localization
            ),
            pixel_size_um=lambda section:self._measurement_context(section)[1],
        )
        self._adaptation = FeedbackAdaptationService(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
            cancel_cgh=lambda key:self._application_session.cancel_cgh(key),
            feedback_orientation=self._geometry.feedback_orientation,
            resolve_position_reference=self._references._resolve_position_reference,
        )
        self._automatic = AutomaticFeedbackRunner(
            runtime_provider=runtime_provider,
            can_run=lambda:bool(self.can_run_automatic_feedback),
            unavailable_reason=lambda:self.automatic_feedback_unavailable_reason,
            is_cgh_computing=(
                lambda key:self._application_session.is_cgh_computing(key)
            ),
            compute_adapted_cgh=(
                lambda *args,**kwargs:
                    self._application_session.compute_adapted_cgh(*args,**kwargs)
            ),
            last_upload_error=lambda:self._application_session.last_upload_error,
            callbacks_provider=callbacks_provider,
            request_measurement=self._measurement.request_measurement,
            cancel_measurement=self._measurement.cancel_measurement,
            feedback_measurement_metadata=self._measurement.feedback_measurement_metadata,
            commit_measurement=self._localization.commit_measurement,
            reuse_localization=self._localization.reuse_localization,
            localize_and_commit=self._localization.localize_and_commit,
            apply_intensity_feedback=self._adaptation.apply_intensity_feedback,
            report_error=self._error,
            report_warning=self._warning,
        )

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

    def observation_planes(self):
        """Return the observation-plane catalog available to feedback."""
        return self._application_session.observation_planes.list()

    def active_observation_plane(self,section_key: str) -> str | None:
        section = str(section_key)
        if section in self._observation_plane_by_section:
            return self._observation_plane_by_section[section]
        planes = self.observation_planes()
        selected = planes[0].name if len(planes) == 1 else None
        self._observation_plane_by_section[section] = selected
        return selected

    def select_observation_plane(self,section_key: str,observation_plane_name: str | None) -> bool:
        """Select feedback measurement context without touching mapping/CGH state."""
        self._require_editor_mode()
        section = str(section_key)
        selected = str(observation_plane_name or "").strip() or None
        if selected is not None:
            self._application_session.observation_planes.get(selected)
        previous = self.active_observation_plane(section)
        if selected == previous:
            return False
        self._measurement.cancel_measurement(section)
        self._application_session.runtime.clear_section_feedback_measurement(section)
        self._observation_plane_by_section[section] = selected
        # A plane change suggests the new plane's detector again; explicit source
        # overrides are workflow-local and do not change observation identity.
        self._source_override_by_section.pop(section,None)
        self._callbacks.on_feedback_state_changed(section)
        return True

    def set_source_override(self,section_key: str,source: str | None) -> bool:
        section = str(section_key)
        value = str(source or "").strip()
        previous = self._source_override_by_section.get(section,"")
        if value == previous:
            return False
        if value:
            self._source_override_by_section[section] = value
        else:
            self._source_override_by_section.pop(section,None)
        self._callbacks.on_feedback_state_changed(section)
        return True

    def _active_feedback_plane(self,section_key: str) -> str | None:
        # Compatibility name used by geometry/reference components. Its semantic
        # identity is the selected observation plane.
        return self.active_observation_plane(section_key)

    def _require_observation_plane_for_measurement(self,section_key: str) -> None:
        if self.observation_planes() and self.active_observation_plane(section_key) is None:
            raise RuntimeError(
                "Select an observation plane before acquiring or loading a measurement."
            )

    def _measurement_context(self,section_key: str) -> tuple[str | None,float | None]:
        observation_plane_name = self.active_observation_plane(section_key)
        if observation_plane_name is None:
            return None,None
        plane = self._application_session.observation_planes.get(observation_plane_name)
        return plane.name,plane.default_pixel_size_um


    # ------------------------------------------------------------------
    # Measurement acquisition
    # ------------------------------------------------------------------

    def available_sources(self,section_key: str) -> Sequence[str]:
        return self._measurement.available_sources(section_key)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        available = tuple(available)
        override = self._source_override_by_section.get(str(section_key))
        if override and override in available:
            return override
        observation_plane_name = self.active_observation_plane(section_key)
        if observation_plane_name is not None:
            detector = self._application_session.observation_planes.get(observation_plane_name).preferred_detector
            if detector and detector in available:
                return detector
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

    def position_reference_selection(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        return self._references.position_reference_selection(section_key)

    def position_reference_context(
        self,section_key: str,
    ) -> PositionReferenceContext:
        return self._references.position_reference_context(section_key)

    def position_reference_presentation(
        self,section_key: str,
    ) -> tuple[PositionReferenceContext,PositionReferencePreview | None]:
        return self._references.position_reference_presentation(section_key)

    def position_reference_records(
        self,observation_plane_name: str,
    ) -> tuple[PositionReferenceRecord,...]:
        return self._references.position_reference_records(observation_plane_name)

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: PositionReferenceMode | str,
        reference_name: str | None=None,
        editable_geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | Sequence[str] | None=None,
        correspondence: Any | None=None,
        correction_mode: str | None=None,
        polynomial_degree: int | None=None,
        affine_transform: Any=_UNSET,
        manual_transform: Any=_UNSET,
        notify: bool=True,
    ) -> PositionReferenceSelection:
        kwargs = {
            "mode":mode,
            "reference_name":reference_name,
            "editable_geometry":editable_geometry,
            "editable_locks":editable_locks,
            "correspondence":correspondence,
            "correction_mode":correction_mode,
            "polynomial_degree":polynomial_degree,
        }
        if affine_transform is not _UNSET:
            kwargs["affine_transform"] = affine_transform
        if manual_transform is not _UNSET:
            kwargs["manual_transform"] = manual_transform
        kwargs["notify"] = bool(notify)
        return self._references.set_position_reference(section_key,**kwargs)

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

    def geometry_orientation_calibration(
        self,section_key: str,*,observation_plane_name: str | None=None,
    ) -> GeometryOrientationCalibration | None:
        return self._geometry.geometry_orientation_calibration(
            section_key,
            observation_plane_name=observation_plane_name,
        )

    def feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        return self._geometry.feedback_orientation(section_key)

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
        self._require_observation_plane_for_measurement(section_key)
        _plane_name,fallback_pixel_size_um = self._measurement_context(section_key)
        if measurement.pixel_size_um is None and fallback_pixel_size_um is not None:
            measurement = replace(measurement,pixel_size_um=fallback_pixel_size_um)
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

    def localize_and_commit(
        self,section_key: str,*,infer_missing_fraction: float | None=None,
    ) -> LocalizationResult:
        return self._localization.localize_and_commit(
            section_key,infer_missing_fraction=infer_missing_fraction,
        )

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

    def dispose(self) -> None:
        self.prepare_runtime_change()
        self._references.dispose()
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

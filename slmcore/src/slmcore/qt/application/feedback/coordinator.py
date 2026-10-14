"""Top-level Qt composition facade for feedback workflows."""

from __future__ import annotations

from typing import Any,Mapping,Sequence,TYPE_CHECKING

from qtpy import QtCore

from ....application.feedback import AutomaticFeedbackState
from ....core.measurement import ImageMeasurement
from ...cgh.session_window import CGHSessionWindow,MeasurementsAction
from .actions import FeedbackActionRouter
from .adaptation import FeedbackAdaptationCoordinator
from .automatic import AutomaticFeedbackCoordinator
from .localization import LocalizationCoordinator
from .measurement import MeasurementCoordinator
from .references import PositionReferenceCoordinator

if TYPE_CHECKING:
    from ....core.cgh.localization import LocalizationResult
    from ....core.cgh.measurement_metrics import IntensityAnalysis
    from ....core.engine.transition import SectionStateTransition
    from ..session import SLMQtSession


_UNSET = object()


class FeedbackCoordinator(QtCore.QObject):
    """Compose focused feedback UI coordinators for one Qt SLM session.

    This is the only feedback UI object that owns a reference to
    :class:`SLMQtSession`. Focused coordinators receive the application feedback
    facade plus narrow Qt callbacks, so measurement/reference/calibration UI
    logic cannot navigate the complete Qt session as a service locator.
    """

    def __init__(self,qt_session: "SLMQtSession") -> None:
        super().__init__(qt_session)
        self._qt_session = qt_session
        self._feedback_service = qt_session.feedback_service
        self._windows: dict[str,CGHSessionWindow] = {}

        flush_section = lambda key:self._qt_session.flush_section(key,propagate=True)
        synchronize_section = self._qt_session.synchronize_section
        editor_writes_allowed = lambda:bool(self._qt_session.editor_writes_allowed)

        self._automatic = AutomaticFeedbackCoordinator(
            self._feedback_service,
            windows=lambda:tuple(self._windows.items()),
            window_for=self.window,
            flush_section=flush_section,
            set_interaction_locked=self._qt_session._set_section_interaction_locked,
            emit_operation_changed=self._qt_session.sigAutomaticOperationChanged.emit,
            report_error=self._error,
        )
        self._measurement = MeasurementCoordinator(
            self._feedback_service,
            window_for=self.window,
            flush_section=flush_section,
            editor_writes_allowed=editor_writes_allowed,
            report_error=self._error,
            refresh_automatic_window=self._automatic.apply_state_to_window,
        )
        self._localization = LocalizationCoordinator(
            self._feedback_service,
            window_for=self.window,
            synchronize_section=synchronize_section,
            report_error=self._error,
        )
        self._references = PositionReferenceCoordinator(
            self._feedback_service,
            window_for=self.window,
            flush_section=flush_section,
            synchronize_section=synchronize_section,
            report_error=self._error,
        )
        self._adaptation = FeedbackAdaptationCoordinator(
            self._feedback_service,
            window_for=self.window,
            runtime=lambda:self._qt_session.runtime,
            flush_section=flush_section,
            synchronize_section=synchronize_section,
            editor_writes_allowed=editor_writes_allowed,
            report_warning=self._warning,
            report_error=self._error,
        )
        self._actions = FeedbackActionRouter(
            measurement=self._measurement,
            localization=self._localization,
            adaptation=self._adaptation,
            references=self._references,
            automatic=self._automatic,
            editor_writes_allowed=editor_writes_allowed,
            compute_adapted_cgh=self._qt_session.compute_adapted_cgh,
            show_inspection=self._show_inspection,
            report_error=self._error,
        )

    @property
    def automatic_operation_active(self) -> bool:
        return self._automatic.operation_active

    def window(self,section_key: str) -> CGHSessionWindow | None:
        return self._windows.get(section_key)

    # ------------------------------------------------------------------
    # Window ownership and synchronization
    # ------------------------------------------------------------------

    def open_window(self,section_key: str) -> None:
        if not self._qt_session.editor_writes_allowed:
            return
        self._qt_session._require_section(section_key)
        try:
            self._qt_session.flush_section(section_key,propagate=True)
            window = self._windows.get(section_key)
            sources = self.available_sources(section_key)
            current = self.preferred_source(section_key,sources)
            observation_planes = tuple(
                plane.name for plane in self._feedback_service.observation_planes()
            )
            current_observation = (
                self._feedback_service.active_observation_plane(section_key)
            )
            if window is None:
                session_inspection=(
                    self._qt_session.runtime.get_section_cgh_session_inspection(
                        section_key
                    )
                )
                parent = self._qt_session.section_collection.section_view(
                    section_key
                ).window()
                window = CGHSessionWindow(
                    status=self._qt_session.runtime.get_section_feedback_status(
                        section_key
                    ),
                    inspection=(
                        self._qt_session.runtime.get_section_feedback_inspection(
                            section_key
                        )
                    ),
                    session_inspection=session_inspection,
                    cgh_status=self._qt_session.runtime.get_section_cgh_status(
                        section_key
                    ),
                    localization_context=self.localization_context(section_key),
                    detectors=sources,
                    current_detector=current,
                    observation_planes=observation_planes,
                    current_observation_plane=current_observation,
                    cgh_summary=self.measurements_cgh_summary(
                        section_key,inspection=session_inspection,
                    ),
                    title="CGH Session - %s/%s" % (
                        self._qt_session.presenter.display_name,section_key,
                    ),
                    parent=parent,
                )
                self._windows[section_key] = window
                window.sigActionRequested.connect(
                    lambda action,options,key=section_key:
                        self._on_action(key,action,options)
                )
                window.sigDefineObservationPlaneRequested.connect(
                    lambda key=section_key:self._define_observation_plane(key)
                )
                window.destroyed.connect(
                    lambda _obj=None,key=section_key:
                        self._windows.pop(key,None)
                )
                self._references.synchronize(section_key,window,lightweight=True)
                self._synchronize_geometry_warning(section_key,window)
            else:
                window.configure_observation_planes(
                    observation_planes,current_observation,
                )
                window.configure_detectors(sources,current)
                self.synchronize_section(section_key)

            self._automatic.configure_availability(window)
            self._automatic.apply_state_to_window(section_key,window)
            window.set_cgh_computing(
                self._qt_session.is_cgh_computing(section_key)
            )
            window.show()
            window.raise_()
            window.activateWindow()
        except Exception as error:
            self._error("Measurements & Corrections failed",error)

    def close_windows(self) -> None:
        for window in tuple(self._windows.values()):
            window.close()
            window.deleteLater()
        self._windows.clear()

    def refresh_observation_planes(self) -> None:
        for section_key in tuple(self._windows):
            self.synchronize_feedback_state(section_key)

    def _define_observation_plane(self,section_key: str) -> None:
        window=self._windows.get(section_key)
        name=self._qt_session._calibration.define_observation_plane(
            section_key=section_key,parent=window,
        )
        if name is None:
            self.synchronize_feedback_state(section_key)
            return
        self._feedback_service.select_observation_plane(section_key,name)

    def synchronize_section(self,section_key: str) -> None:
        window = self._windows.get(section_key)
        if window is None:
            return
        runtime = self._qt_session.runtime
        observation_planes = tuple(
            plane.name for plane in self._feedback_service.observation_planes()
        )
        current_observation = self._feedback_service.active_observation_plane(
            section_key
        )
        sources = self.available_sources(section_key)
        session_inspection=runtime.get_section_cgh_session_inspection(section_key)
        window.configure_observation_planes(observation_planes,current_observation)
        window.configure_detectors(
            sources,self.preferred_source(section_key,sources),
        )
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            session_inspection,
            runtime.get_section_cgh_status(section_key),
            self.localization_context(section_key),
            self.measurements_cgh_summary(
                section_key,inspection=session_inspection,
            ),
        )
        self._references.synchronize(section_key,window,lightweight=True)
        self._synchronize_geometry_warning(section_key,window)
        self._automatic.configure_availability(window)
        self._automatic.apply_state_to_window(section_key,window)

    def synchronize_feedback_state(self,section_key: str) -> None:
        """Refresh feedback/measurement state without rebuilding CGH round inspection."""
        window=self._windows.get(section_key)
        if window is None:
            return
        runtime=self._qt_session.runtime
        observation_planes=tuple(
            plane.name for plane in self._feedback_service.observation_planes()
        )
        current_observation=self._feedback_service.active_observation_plane(
            section_key
        )
        sources=self.available_sources(section_key)
        window.configure_observation_planes(
            observation_planes,current_observation,
        )
        window.configure_detectors(
            sources,self.preferred_source(section_key,sources),
        )
        window.set_feedback_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            self.localization_context(section_key),
        )
        self._references.synchronize(section_key,window,lightweight=True)
        self._synchronize_geometry_warning(section_key,window)
        self._automatic.configure_availability(window)
        self._automatic.apply_state_to_window(section_key,window)

    def synchronize_position_reference(self,section_key: str) -> None:
        """Refresh only position-reference presentation for one open window."""
        window=self._windows.get(section_key)
        if window is not None:
            self._references.synchronize(section_key,window,lightweight=True)

    def _synchronize_geometry_warning(
        self,section_key: str,window: CGHSessionWindow,
    ) -> None:
        plane=self._feedback_service.active_observation_plane(section_key)
        calibrated=bool(
            plane
            and self._feedback_service.geometry_orientation_calibration(
                section_key,observation_plane_name=plane,
            ) is not None
        )
        window.set_geometry_calibration_status(
            observation_plane_name=plane,calibrated=calibrated,
        )

    def set_cgh_computing(self,section_key: str,computing: bool) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.set_cgh_computing(bool(computing))
            self._automatic.apply_state_to_window(section_key,window)

    def measurements_cgh_summary(
        self,section_key: str,*,inspection: Any | None=None,
    ) -> Mapping[str,Any]:
        runtime = self._qt_session.runtime
        if inspection is None:
            inspection = runtime.get_section_cgh_session_inspection(section_key)
        target_state = None
        working = inspection.working_round
        if working is not None and working.target_state is not None:
            target_state = working.target_state
        elif inspection.committed_target is not None:
            target_state = inspection.committed_target

        state = runtime.get_section_state_copy(section_key).cgh
        if target_state is None:
            target_key = state.selected_target
            if target_key is None or target_key not in state.items:
                return {}
            target_params = state.items[target_key].params.values
        else:
            target_key = target_state.target_type
            target_params = target_state.canonical_params

        summary = dict(self._qt_session._target_presentation_summary(
            section_key,target_key,target_params,
        ))
        if state.selected_target in state.items:
            target = state.items[state.selected_target]
            summary.update({
                "algorithm":str(target.algorithm),
                "parameters":dict(target.computation.params.values),
            })
        return summary

    def _show_inspection(self,section_key: str) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.show_inspection(
                self._qt_session.runtime.get_section_feedback_inspection(section_key)
            )

    def _on_action(
        self,section_key: str,action: str,options: Mapping[str,Any],
    ) -> None:
        self._actions.dispatch(section_key,action,options)

    # ------------------------------------------------------------------
    # Delegated measurement/localization API
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
        on_result,
        on_error,
    ) -> None:
        self._measurement.request_measurement(
            section_key,source,metadata=metadata,
            on_result=on_result,on_error=on_error,
        )

    def cancel_measurement(self,section_key: str) -> None:
        self._measurement.cancel_measurement(section_key)

    def acquire(
        self,section_key: str,source: str,*,reuse_previous_localization: bool,
    ) -> None:
        self._measurement.acquire(
            section_key,source,
            reuse_previous_localization=reuse_previous_localization,
        )

    def load(
        self,section_key: str,*,reuse_previous_localization: bool,
    ) -> None:
        self._measurement.load(
            section_key,
            reuse_previous_localization=reuse_previous_localization,
        )

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        return self._measurement.commit_measurement(
            section_key,measurement,
            reuse_previous_localization=reuse_previous_localization,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        return self._measurement.feedback_measurement_metadata(section_key)

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        return self._localization.localization_candidate(section_key,parameters)

    def run_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        self._localization.run_localization_candidate(section_key,parameters)

    def infer_missing_localization_candidate(
        self,section_key: str,localization: LocalizationResult,parameters: Mapping[str,Any],
    ) -> None:
        self._localization.infer_missing_localization_candidate(
            section_key,localization,parameters,
        )

    def accept_localization(
        self,
        section_key: str,
        localization: LocalizationResult,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> None:
        self._localization.accept_localization(
            section_key,localization,parameters,raise_errors=raise_errors,
        )

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> LocalizationResult | None:
        return self._localization.reuse_localization(
            section_key,raise_errors=raise_errors,
        )

    def localize_and_commit(self,section_key: str) -> None:
        self._localization.localize_and_commit(section_key)

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: LocalizationResult | None=None,
        localization_parameters: Mapping[str,Any] | None=None,
    ) -> None:
        self._localization.update_parameters(
            section_key,group,changes,
            localization=localization,
            localization_parameters=localization_parameters,
        )

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        return self._localization.localization_context(section_key)

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        self._localization.require_current_cgh_for_localization_commit(section_key)

    def validate_target_hint_use(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        self._localization.validate_target_hint_use(section_key,parameters)

    # ------------------------------------------------------------------
    # Delegated geometry/reference workflows
    # ------------------------------------------------------------------

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: str,
        reference_name: str | None,
        editable_geometry: Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | None=None,
        correspondence: str | None=None,
        correction_mode: str | None=None,
        polynomial_degree: int | None=None,
        affine_transform: Any=_UNSET,
        manual_transform: Any=_UNSET,
    ) -> None:
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
        self._references.set_position_reference(section_key,**kwargs)

    def fit_position_reference_center(self,section_key: str) -> None:
        self._references.fit_position_reference_center(section_key)

    def add_position_reference(self,section_key: str,name: str) -> None:
        self._references.add_position_reference(section_key,name)

    def persist_position_reference(self,section_key: str,name: str) -> None:
        self._references.persist_position_reference(section_key,name)

    def delete_position_reference(self,section_key: str,name: str) -> None:
        self._references.delete_position_reference(section_key,name)

    # ------------------------------------------------------------------
    # Delegated feedback adaptation API
    # ------------------------------------------------------------------

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> tuple[bool,SectionStateTransition | None]:
        return self._adaptation.apply_intensity_feedback(
            section_key,raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str) -> None:
        self._adaptation.reset_intensity_feedback(section_key)

    def apply_position_correction(self,section_key: str) -> None:
        self._adaptation.apply_position_correction(section_key)

    def set_position_active(self,section_key: str,active: bool) -> None:
        self._adaptation.set_position_active(section_key,active)

    def set_position_active_from_dashboard(
        self,section_key: str,active: bool,
    ) -> None:
        """Route the compact CGH checkbox through normal feedback actions."""
        self._actions.dispatch(
            section_key,MeasurementsAction.POSITION_SET_ACTIVE.value,
            {"active":bool(active)},
        )

    def clear_position_correction(self,section_key: str) -> None:
        self._adaptation.clear_position_correction(section_key)

    def reset_to_round(self,section_key: str,round_index: int) -> None:
        self._adaptation.reset_to_round(section_key,round_index)

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ) -> None:
        self._adaptation.propagate_round(
            section_key,round_index,
            position_context=position_context,pad_size=pad_size,
        )

    # ------------------------------------------------------------------
    # Application callbacks and lifecycle
    # ------------------------------------------------------------------

    def on_automatic_state_changed(self,state: AutomaticFeedbackState) -> None:
        self._automatic.on_state_changed(state)

    def on_automatic_finished(self,section_key: str,message: str) -> None:
        self._automatic.on_finished(section_key,message)

    def on_measurement_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        self._measurement.on_busy_changed(section_key,busy,message)

    def on_measurement_error(self,section_key: str,error: Exception) -> None:
        self._measurement.on_error(section_key,error)

    def on_localization_error(self,section_key: str,error: Exception) -> None:
        self._localization.on_error(section_key,error)

    def _set_measurement_error(self,section_key: str,error: Exception) -> None:
        self._measurement.set_error(section_key,error)

    def _set_localization_error(self,section_key: str,error: Exception) -> None:
        self._localization.set_error(section_key,error)

    def _warning(self,title: str,message: Any) -> None:
        self._qt_session.sigWarning.emit(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        self._qt_session._emit_exception(str(title),error)

    def refresh_automatic_availability(self) -> None:
        self._automatic.refresh_availability()

    def prepare_runtime_replacement(self) -> None:
        self.close_windows()

    def runtime_replaced(self) -> None:
        pass

    def dispose(self) -> None:
        self.close_windows()

"""Feedback orientation and geometry-orientation calibration workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping,TYPE_CHECKING,TypedDict,cast

import numpy as np

from ...core.cgh.feedback import (
    FeedbackOrientation,GeometryOrientationCalibration,GeometryOrientationAnalysis,
    analyze_geometry_orientation,orient_localization,
)
from ...core.cgh.localization import LocalizationResult,localize_measurement
from ...core.cgh.coordinates import reference_px_to_k,k_to_reference_px
from ...core.measurement import ImageMeasurement
from .component import FeedbackComponent
from .contracts import FeedbackOrientationContext

if TYPE_CHECKING:
    from ...core.calibration.slm_section_calibration import SLMSectionCalibration
    from ...core.cgh.targets.resolution import TargetResolution
    from ...workspace.geometry_orientation_calibration_store import (
        SLMGeometryOrientationCalibrationStore,
    )
    from .service import SLMFeedbackService


class GeometryCalibrationPayload(TypedDict):
    """Detached geometry CGH produced by SLMSession without experiment commit."""

    frame: np.ndarray
    localization_context: Mapping[str,Any]
    target_intensities: np.ndarray
    lattice_indices: np.ndarray
    target_type: str
    target_params: dict[str,Any]
    resolution: TargetResolution
    calibration: SLMSectionCalibration | None

class GeometryOrientationCalibrationContext(TypedDict):
    """Mapping consumed by geometry controls for the active section and plane."""

    plane_name: str | None
    calibrated: bool
    calibration: GeometryOrientationCalibration | None
    grid_x: int
    grid_y: int
    period_x_px: float
    period_y_px: float
    period_x_um: float | None
    period_y_um: float | None
    metric_periods_available: bool
    computed: bool
    frame_active: bool
    any_frame_active: bool
    active_frame_plane: str | None
    measurement: ImageMeasurement | None
    localization: LocalizationResult | None
    localization_parameters: dict[str,Any]
    localization_context: dict[str,Any]
    analysis: GeometryOrientationAnalysis | None
    analyze_enabled: bool
    save_enabled: bool
    detector_name: str | None

@dataclass
class _GeometryCalibrationWorkflow:
    """Manual section/plane workflow, retaining its target until explicit finish."""

    grid_x: int=5
    grid_y: int=4
    period_x_px: float | None=None
    period_y_px: float | None=None
    payload: GeometryCalibrationPayload | None=None
    measurement: ImageMeasurement | None=None
    localization: LocalizationResult | None=None
    localization_parameters: dict[str,Any] | None=None
    analysis: GeometryOrientationAnalysis | None=None
    detector_name: str | None=None
    frame_active: bool=False

class GeometryOrientationFeedbackService(FeedbackComponent):
    """Own transient orientation state and geometry-calibration workflows."""

    def __init__(
        self,feedback: "SLMFeedbackService",
        store: "SLMGeometryOrientationCalibrationStore | None"=None,
    ) -> None:
        super().__init__(feedback)
        self._geometry_orientation_calibration_store = store
        self._feedback_orientations: dict[
            str,tuple[str | None,FeedbackOrientation]
        ] = {}
        self._geometry_calibration_workflows: dict[
            tuple[str,str | None],_GeometryCalibrationWorkflow
        ] = {}
        if self._geometry_orientation_calibration_store is not None:
            self._geometry_orientation_calibration_store.add_listener(
                self._on_geometry_orientation_calibration_store_changed
            )

    def _saved_feedback_orientation(
        self,section_key: str,plane_name: str | None,
    ) -> FeedbackOrientation:
        calibration = self.geometry_orientation_calibration(
            section_key,plane_name=plane_name,
        )
        if calibration is not None:
            return calibration.orientation
        preferences = self._application_session.startup_preferences
        value = (
            "identity" if preferences is None
            else preferences.feedback_orientation(section_key,plane_name)
        )
        return FeedbackOrientation.normalize(value)

    def geometry_orientation_calibration(
        self,section_key: str,*,plane_name: str | None=None,
    ) -> GeometryOrientationCalibration | None:
        """Load the saved orientation for this SLM/section and active or given plane."""
        section=str(section_key)
        plane=(self._active_feedback_plane(section) if plane_name is None else str(plane_name or '').strip() or None)
        store=self._geometry_orientation_calibration_store
        if plane is None or store is None:
            return None
        identity=self._application_session.runtime.identity
        if not store.exists(identity,section,plane):
            return None
        return store.load(identity,section,plane)

    def _geometry_calibration_key(self,section_key: str) -> tuple[str,str | None]:
        section=str(section_key)
        return section,self._active_feedback_plane(section)

    def _active_geometry_calibration_frame(
        self,section_key: str,
    ) -> tuple[str | None,_GeometryCalibrationWorkflow] | None:
        section=str(section_key)
        for (workflow_section,plane),workflow in self._geometry_calibration_workflows.items():
            if workflow_section == section and workflow.frame_active:
                return plane,workflow
        return None

    def _geometry_period_guess_px(
        self,section_key: str,grid_x: int,grid_y: int,
    ) -> tuple[float,float]:
        state=self._application_session.runtime.get_section_state_copy(section_key)
        selected=state.cgh.selected_target
        params={}
        if selected is not None and selected in state.cgh.items:
            params=dict(state.cgh.items[selected].params.values)
        fx=float(params.get('fov_x_px') or 0.0)
        fy=float(params.get('fov_y_px') or 0.0)
        if fx > 0 and fy > 0:
            return (0.5*fx/max(1,int(grid_x)-1),0.5*fy/max(1,int(grid_y)-1))
        px=float(params.get('period_x_px') or 6.0)
        py=float(params.get('period_y_px') or 6.0)
        return px,py

    def _geometry_period_to_um(self,section_key: str,value: float,axis: str) -> float | None:
        calibration=self._application_session.runtime.get_section_calibration_copy(section_key)
        if calibration is None or not calibration.is_valid():
            return None
        k=reference_px_to_k(float(value))
        x,y=((k,0.0) if axis == 'x' else (0.0,k))
        dx,dy=calibration.kxy_to_um(x,y)
        return float(dx if axis == 'x' else dy)

    def geometry_period_from_um(self,section_key: str,value_um: float,axis: str) -> float:
        """Convert a metric period to reference pixels using the section calibration."""
        calibration=self._application_session.runtime.get_section_calibration_copy(section_key)
        if calibration is None or not calibration.is_valid():
            raise RuntimeError('A valid section/plane FOV calibration is required for metric geometry periods.')
        x,y=((float(value_um),0.0) if axis == 'x' else (0.0,float(value_um)))
        kx,ky=calibration.um_to_kxy(x,y)
        return float(k_to_reference_px(kx if axis == 'x' else ky))

    def geometry_orientation_calibration_context(
        self,section_key: str,
    ) -> GeometryOrientationCalibrationContext:
        """Inspect the active plane workflow, creating default draft geometry if absent."""
        section=str(section_key)
        plane=self._active_feedback_plane(section)
        key=(section,plane)
        workflow=self._geometry_calibration_workflows.get(key)
        if workflow is None:
            gx,gy=5,4
            px,py=self._geometry_period_guess_px(section,gx,gy)
            workflow=_GeometryCalibrationWorkflow(grid_x=gx,grid_y=gy,period_x_px=px,period_y_px=py)
            self._geometry_calibration_workflows[key]=workflow
        calibration=self.geometry_orientation_calibration(section,plane_name=plane)
        active_frame=self._active_geometry_calibration_frame(section)
        active_frame_plane=(None if active_frame is None else active_frame[0])
        px=float(workflow.period_x_px or self._geometry_period_guess_px(section,workflow.grid_x,workflow.grid_y)[0])
        py=float(workflow.period_y_px or self._geometry_period_guess_px(section,workflow.grid_x,workflow.grid_y)[1])
        return {
            'plane_name':plane,
            'calibrated':calibration is not None,
            'calibration':calibration,
            'grid_x':int(workflow.grid_x),'grid_y':int(workflow.grid_y),
            'period_x_px':px,'period_y_px':py,
            'period_x_um':self._geometry_period_to_um(section,px,'x'),
            'period_y_um':self._geometry_period_to_um(section,py,'y'),
            'metric_periods_available':self._geometry_period_to_um(section,px,'x') is not None and self._geometry_period_to_um(section,py,'y') is not None,
            'computed':workflow.payload is not None,
            'frame_active':bool(workflow.frame_active),
            'any_frame_active':active_frame is not None,
            'active_frame_plane':active_frame_plane,
            'measurement':workflow.measurement,
            'localization':workflow.localization,
            'localization_parameters':dict(workflow.localization_parameters or {}),
            'localization_context':({} if workflow.payload is None else dict(workflow.payload.get('localization_context') or {})),
            'analysis':workflow.analysis,
            'analyze_enabled':workflow.measurement is not None and workflow.localization is not None,
            'save_enabled':workflow.analysis is not None and bool(workflow.analysis.accepted),
            'detector_name':workflow.detector_name,
        }

    def _feedback_orientation_change_reason(self,section_key: str) -> str:
        status = self._application_session.runtime.get_section_cgh_status(section_key)
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
        preferences = self._application_session.startup_preferences
        plane_override = bool(
            plane
            and preferences is not None
            and preferences.feedback_orientation_for_plane(section,plane) is not None
        )
        reason = self._feedback_orientation_change_reason(section)
        return FeedbackOrientationContext(
            orientation=orientation,
            saved_orientation=saved,
            plane_name=plane,
            plane_override=plane_override,
            calibrated=self.geometry_orientation_calibration(section,plane_name=plane) is not None,
            change_allowed=not bool(reason),
            change_unavailable_reason=reason,
        )

    def compute_geometry_orientation_calibration_target(
        self,section_key: str,*,grid_x: int,grid_y: int,period_x_px: float,period_y_px: float,
        source: str,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        """Compute/upload a detached target, replacing workflow state only after success.

        Acquisition is explicit. Failed computation/upload retains the previous
        workflow; finish_geometry_orientation_calibration restores the experiment.
        """
        self._require_editor_mode()
        section=str(section_key); plane=self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError('Select a measurement plane before geometry calibration.')
        key=(section,plane)
        workflow=self._geometry_calibration_workflows.setdefault(key,_GeometryCalibrationWorkflow())
        pending_grid_x=int(grid_x); pending_grid_y=int(grid_y)
        pending_period_x=float(period_x_px); pending_period_y=float(period_y_px)
        pending_detector=str(source or '').strip() or None

        def finished(
            success: bool,error: Exception | None,payload: GeometryCalibrationPayload | None,
        ) -> None:
            if not success or payload is None:
                if on_complete is not None:on_complete(False,error or RuntimeError('Geometry calibration CGH failed'))
                return
            candidate_payload=cast(GeometryCalibrationPayload,dict(payload))
            if not self._application_session.upload_frame(candidate_payload['frame']):
                if on_complete is not None:
                    on_complete(False,RuntimeError('Could not upload the geometry calibration frame.'))
                return
            for (workflow_section,_workflow_plane),other in self._geometry_calibration_workflows.items():
                if workflow_section == section:
                    other.frame_active=False
            workflow.grid_x=pending_grid_x; workflow.grid_y=pending_grid_y
            workflow.period_x_px=pending_period_x; workflow.period_y_px=pending_period_y
            workflow.payload=candidate_payload
            workflow.measurement=None; workflow.localization=None
            workflow.localization_parameters=None; workflow.analysis=None
            workflow.detector_name=pending_detector
            workflow.frame_active=True
            self._section_changed(section)
            if on_complete is not None:on_complete(True,None)

        return self._application_session.compute_geometry_orientation_cgh(
            section,grid_x=int(grid_x),grid_y=int(grid_y),
            period_x_px=float(period_x_px),period_y_px=float(period_y_px),
            on_finished=finished,
        )

    def acquire_geometry_orientation_calibration(
        self,section_key: str,source: str,*,on_complete: Callable[[bool,Exception | None],None] | None=None,
    ) -> None:
        """Acquire for the displayed target, clearing its previous localization/analysis."""
        self._require_editor_mode()
        section=str(section_key); key=self._geometry_calibration_key(section)
        workflow=self._geometry_calibration_workflows.get(key)
        if workflow is None or workflow.payload is None:
            raise RuntimeError('Compute the geometry calibration target before acquiring.')
        if not workflow.frame_active:
            raise RuntimeError(
                'The geometry calibration target is not currently displayed. '
                'Compute the calibration target again before acquiring.'
            )
        source=str(source or '').strip()
        if not source:raise ValueError('Select a detector before acquisition.')
        workflow.detector_name=source

        def result(measurement: ImageMeasurement) -> None:
            workflow.measurement=measurement
            workflow.localization=None; workflow.localization_parameters=None
            workflow.analysis=None
            workflow.detector_name=(
                str(getattr(measurement,"detector",None) or source).strip() or None
            )
            self._section_changed(section)
            if on_complete is not None:on_complete(True,None)

        def error(error: Exception) -> None:
            self._section_changed(section)
            if on_complete is not None:on_complete(False,error)

        self.request_measurement(
            section,source,
            metadata={**self.feedback_measurement_metadata(section),'workflow':'geometry_orientation_calibration'},
            on_result=result,on_error=error,
        )

    def set_geometry_orientation_calibration_measurement(
        self,section_key: str,measurement: ImageMeasurement,
    ) -> None:
        """Load a measurement for a computed target without changing the displayed frame."""
        self._require_editor_mode(); section=str(section_key)
        workflow=self._geometry_calibration_workflows.get(self._geometry_calibration_key(section))
        if workflow is None or workflow.payload is None:
            raise RuntimeError('Compute the geometry calibration target before loading a measurement.')
        workflow.measurement=measurement
        workflow.localization=None; workflow.localization_parameters=None
        workflow.analysis=None
        self._section_changed(section)

    def _localize_geometry_orientation_workflow(
        self,section_key: str,workflow: _GeometryCalibrationWorkflow,parameters: Mapping[str,Any] | None,
    ) -> LocalizationResult:
        """Replace this draft's localization and invalidate its previous orientation fit."""
        if workflow.payload is None or workflow.measurement is None:
            raise RuntimeError('Geometry calibration needs a computed target and measurement.')
        payload=workflow.payload
        if parameters is None:
            parameters=dict(
                self._application_session.runtime.get_section_feedback_status(
                    section_key
                ).localization_params
            )
            parameters.update(suggest_localization_sources(
                workflow.measurement,payload.get('localization_context') or {},allow_target_hints=True,
            ))
        candidate=localize_measurement(
            workflow.measurement,target_type=str(payload['target_type']),
            target_params=dict(payload['target_params']),resolution=payload['resolution'],
            parameters=dict(parameters),calibration=payload.get('calibration'),
        )
        workflow.localization=candidate
        workflow.localization_parameters=dict(parameters)
        workflow.analysis=None
        return candidate

    def geometry_orientation_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> LocalizationResult:
        """Localize the calibration measurement and retain the candidate in its draft."""
        self._require_editor_mode(); section=str(section_key)
        workflow=self._geometry_calibration_workflows.get(self._geometry_calibration_key(section))
        if workflow is None:raise RuntimeError('Start geometry calibration first.')
        candidate=self._localize_geometry_orientation_workflow(section,workflow,parameters)
        self._section_changed(section)
        return candidate

    def analyze_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationAnalysis:
        """Analyze the localized calibration target without saving or restoring its frame."""
        self._require_editor_mode(); section=str(section_key)
        workflow=self._geometry_calibration_workflows.get(self._geometry_calibration_key(section))
        if workflow is None or workflow.payload is None or workflow.measurement is None or workflow.localization is None:
            raise RuntimeError('Acquire/load and localize the geometry calibration target first.')
        analysis=analyze_geometry_orientation(
            workflow.measurement,workflow.localization,
            np.asarray(workflow.payload['target_intensities'],dtype=np.float64),
        )
        workflow.analysis=analysis
        self._section_changed(section)
        return analysis

    def save_geometry_orientation_calibration(self,section_key: str) -> GeometryOrientationCalibration:
        """Persist an accepted orientation and refresh feedback; leave target displayed."""
        self._require_editor_mode(); section=str(section_key); plane=self._active_feedback_plane(section)
        if plane is None:raise RuntimeError('Select a measurement plane first.')
        workflow=self._geometry_calibration_workflows.get((section,plane))
        if workflow is None or workflow.analysis is None or not workflow.analysis.accepted:
            raise RuntimeError('Analyze a confident geometry calibration before saving.')
        store=self._geometry_orientation_calibration_store
        if store is None:raise RuntimeError('Geometry orientation-calibration storage is unavailable.')
        calibration=GeometryOrientationCalibration(
            slm_serial=self._application_session.runtime.identity.serial_number,section_key=section,plane_name=plane,
            orientation=workflow.analysis.orientation,detector_name=workflow.detector_name,
            grid_x=workflow.grid_x,grid_y=workflow.grid_y,
            period_x_px=float(workflow.period_x_px),period_y_px=float(workflow.period_y_px),
            score=workflow.analysis.score,confidence_margin=workflow.analysis.confidence_margin,
            matched_count=workflow.analysis.matched_count,
            provenance={'measurement_id':getattr(workflow.measurement,'measurement_id',None)},
        )
        store.save(calibration)
        self._feedback_orientations[section]=(plane,calibration.orientation)
        if self._application_session.runtime.get_section_feedback_status(section).localization_available:
            self._update_committed_analysis(section)
        self._section_changed(section)
        return calibration

    def finish_geometry_orientation_calibration(self,section_key: str) -> None:
        """Restore the authoritative experiment frame after manual calibration."""
        self._require_editor_mode()
        section=str(section_key)
        active=self._active_geometry_calibration_frame(section)
        if active is None:
            return
        _plane,workflow=active
        if not self._application_session.upload_current_frame():
            raise RuntimeError('Could not restore the current experiment CGH.')
        workflow.frame_active=False
        self._section_changed(section)

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
        if self._application_session.runtime.get_section_feedback_status(
            section
        ).localization_available:
            self._update_committed_analysis(section)
        self._section_changed(section)
        return normalized

    def save_feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        """Persist the current transient orientation for the active plane/default."""
        self._require_editor_mode()
        section = str(section_key)
        orientation = self.feedback_orientation(section)
        plane = self._active_feedback_plane(section)
        preferences = self._application_session.startup_preferences
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        if plane is None:
            preferences.set_feedback_orientation_default(section,orientation.value)
        else:
            preferences.set_feedback_orientation_for_plane(
                section,plane,orientation.value,
            )
        self._section_changed(section)
        return orientation

    def refresh_feedback_orientation_contexts(self,*,force: bool=False) -> None:
        """Refresh orientation after plane changes or authoritative calibration changes."""
        for section_key in tuple(self._application_session.runtime.section_keys):
            section = str(section_key)
            current = self._feedback_orientations.get(section)
            if current is None:
                continue
            plane = self._active_feedback_plane(section)
            if current[0] == plane and not force:
                continue
            orientation = self._saved_feedback_orientation(section,plane)
            self._feedback_orientations[section] = (plane,orientation)
            if self._application_session.runtime.get_section_feedback_status(
                section
            ).localization_available:
                self._update_committed_analysis(section)
            self._section_changed(section)

    def prepare_runtime_change(self) -> None:
        """Clear transient orientation overrides while preserving geometry drafts."""
        self._feedback_orientations.clear()

    def dispose(self) -> None:
        if self._geometry_orientation_calibration_store is not None:
            self._geometry_orientation_calibration_store.remove_listener(
                self._on_geometry_orientation_calibration_store_changed
            )
        self._feedback_orientations.clear()
        self._geometry_calibration_workflows.clear()

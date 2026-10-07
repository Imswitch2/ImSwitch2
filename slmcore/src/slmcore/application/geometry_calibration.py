"""Reusable section × observation-plane geometry-calibration workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,Mapping,TYPE_CHECKING,TypedDict,cast

import numpy as np

from ..core.cgh.coordinates import reference_px_to_k,k_to_reference_px
from ..core.cgh.feedback import (
    FeedbackOrientation,GeometryOrientationAnalysis,GeometryOrientationCalibration,
    analyze_geometry_orientation,
)
from ..core.cgh.localization import (
    LocalizationResult,localize_measurement,suggest_localization_sources,
)
from ..core.measurement import ImageMeasurement
from .feedback.contracts import MeasurementDispatcher,MeasurementRequest

if TYPE_CHECKING:
    from ..core.calibration.slm_section_calibration import SLMSectionCalibration
    from ..core.cgh.targets.resolution import TargetResolution
    from .observation import ObservationPlaneService
    from .observation_geometry import ObservationGeometryService
    from .session import SLMSession


class GeometryCalibrationPayload(TypedDict):
    """Detached geometry CGH produced without committing experiment state."""

    frame: np.ndarray
    localization_context: Mapping[str,Any]
    target_intensities: np.ndarray
    lattice_indices: np.ndarray
    target_type: str
    target_params: dict[str,Any]
    resolution: TargetResolution
    calibration: SLMSectionCalibration | None


class GeometryCalibrationContext(TypedDict):
    observation_plane_name: str | None
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
    active_frame_observation_plane: str | None
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


class GeometryCalibrationService:
    """Reusable geometry calibration independent of Feedback UI/state selection."""

    def __init__(
        self,
        session: "SLMSession",
        *,
        observation_planes: "ObservationPlaneService",
        observation_geometry: "ObservationGeometryService",
        measurements: MeasurementDispatcher | None=None,
    ) -> None:
        self.session=session
        self.observation_planes=observation_planes
        self.observation_geometry=observation_geometry
        self.measurements=measurements
        self._workflows: dict[tuple[str,str],_GeometryCalibrationWorkflow]={}
        self._requests: dict[str,MeasurementRequest]={}

    def set_measurement_dispatcher(self,measurements: MeasurementDispatcher | None) -> None:
        if measurements is self.measurements:
            return
        self.cancel_all()
        self.measurements=measurements

    def observation_plane_names(self) -> tuple[str,...]:
        return tuple(plane.name for plane in self.observation_planes.list())

    def _require_editor_mode(self) -> None:
        if not self.session.editor_writes_allowed:
            raise RuntimeError("Geometry calibration is available only in editor mode. Reopen the SLM session with calibration editing enabled, then retry.")

    def _require_plane(self,observation_plane_name: str | None) -> str:
        plane=str(observation_plane_name or "").strip()
        if not plane:
            raise RuntimeError("No observation plane is selected in the Calibration workspace. Select an observation plane on the left, then retry geometry calibration.")
        self.observation_planes.get(plane)
        return plane

    def _key(self,section_key: str,observation_plane_name: str | None) -> tuple[str,str]:
        return str(section_key),self._require_plane(observation_plane_name)

    def _active_frame(self,section_key: str):
        section=str(section_key)
        for (workflow_section,plane),workflow in self._workflows.items():
            if workflow_section==section and workflow.frame_active:
                return plane,workflow
        return None

    def _period_guess_px(self,section_key: str,grid_x: int,grid_y: int) -> tuple[float,float]:
        state=self.session.runtime.get_section_state_copy(section_key)
        selected=state.cgh.selected_target
        params={}
        if selected is not None and selected in state.cgh.items:
            params=dict(state.cgh.items[selected].params.values)
        fx=float(params.get("fov_x_px") or 0.0)
        fy=float(params.get("fov_y_px") or 0.0)
        if fx>0 and fy>0:
            return 0.5*fx/max(1,int(grid_x)-1),0.5*fy/max(1,int(grid_y)-1)
        return float(params.get("period_x_px") or 6.0),float(params.get("period_y_px") or 6.0)

    def _period_to_um(self,section_key: str,value: float,axis: str) -> float | None:
        calibration=self.session.runtime.get_section_calibration_copy(section_key)
        if calibration is None or not calibration.is_valid():
            return None
        k=reference_px_to_k(float(value))
        x,y=((k,0.0) if axis=="x" else (0.0,k))
        dx,dy=calibration.kxy_to_um(x,y)
        return float(dx if axis=="x" else dy)

    def period_from_um(self,section_key: str,value_um: float,axis: str) -> float:
        calibration=self.session.runtime.get_section_calibration_copy(section_key)
        if calibration is None or not calibration.is_valid():
            raise RuntimeError(
                "Metric geometry periods require a base mapping calibration. Calibrate/select the mapping first, or use reference-pixel periods instead."
            )
        x,y=((float(value_um),0.0) if axis=="x" else (0.0,float(value_um)))
        kx,ky=calibration.um_to_kxy(x,y)
        return float(k_to_reference_px(kx if axis=="x" else ky))

    def context(self,section_key: str,observation_plane_name: str | None) -> GeometryCalibrationContext:
        section=str(section_key)
        plane=str(observation_plane_name or "").strip() or None
        if plane is None:
            gx,gy=5,4
            px,py=self._period_guess_px(section,gx,gy)
            active=self._active_frame(section)
            px_um=self._period_to_um(section,px,"x")
            py_um=self._period_to_um(section,py,"y")
            return {
                "observation_plane_name":None,"calibrated":False,"calibration":None,
                "grid_x":gx,"grid_y":gy,"period_x_px":px,"period_y_px":py,
                "period_x_um":px_um,"period_y_um":py_um,
                "metric_periods_available":px_um is not None and py_um is not None,
                "computed":False,"frame_active":False,"any_frame_active":active is not None,
                "active_frame_observation_plane":None if active is None else active[0],
                "measurement":None,"localization":None,"localization_parameters":{},
                "localization_context":{},"analysis":None,"analyze_enabled":False,
                "save_enabled":False,"detector_name":None,
            }
        self.observation_planes.get(plane)
        workflow=self._workflows.get((section,plane))
        if workflow is None:
            gx,gy=5,4
            px,py=self._period_guess_px(section,gx,gy)
            workflow=_GeometryCalibrationWorkflow(grid_x=gx,grid_y=gy,period_x_px=px,period_y_px=py)
            self._workflows[(section,plane)]=workflow
        calibration=self.observation_geometry.calibration(section,plane)
        active=self._active_frame(section)
        px=float(workflow.period_x_px or self._period_guess_px(section,workflow.grid_x,workflow.grid_y)[0])
        py=float(workflow.period_y_px or self._period_guess_px(section,workflow.grid_x,workflow.grid_y)[1])
        px_um=self._period_to_um(section,px,"x")
        py_um=self._period_to_um(section,py,"y")
        return {
            "observation_plane_name":plane,"calibrated":calibration is not None,"calibration":calibration,
            "grid_x":int(workflow.grid_x),"grid_y":int(workflow.grid_y),
            "period_x_px":px,"period_y_px":py,
            "period_x_um":px_um,"period_y_um":py_um,
            "metric_periods_available":px_um is not None and py_um is not None,
            "computed":workflow.payload is not None,"frame_active":bool(workflow.frame_active),
            "any_frame_active":active is not None,
            "active_frame_observation_plane":None if active is None else active[0],
            "measurement":workflow.measurement,"localization":workflow.localization,
            "localization_parameters":dict(workflow.localization_parameters or {}),
            "localization_context":{} if workflow.payload is None else dict(workflow.payload.get("localization_context") or {}),
            "analysis":workflow.analysis,
            "analyze_enabled":workflow.measurement is not None and workflow.localization is not None,
            "save_enabled":workflow.analysis is not None and bool(workflow.analysis.accepted),
            "detector_name":workflow.detector_name,
        }

    def available_sources(self,section_key: str) -> tuple[str,...]:
        m=self.measurements
        if m is None or not m.available:
            return ()
        try:
            return tuple(str(value).strip() for value in m.available_sources(section_key) if str(value).strip())
        except Exception:
            return ()

    def preferred_source(self,section_key: str,observation_plane_name: str | None) -> str | None:
        plane=self._require_plane(observation_plane_name)
        sources=self.available_sources(section_key)
        preferred=self.observation_planes.get(plane).preferred_detector
        if preferred in sources:
            return preferred
        m=self.measurements
        try:
            value=None if m is None else m.preferred_source(section_key,sources)
        except Exception:
            value=None
        value=str(value or "").strip() or None
        return value if value in sources else (sources[0] if sources else None)

    def compute_target(
        self,section_key: str,observation_plane_name: str,*,grid_x: int,grid_y: int,
        period_x_px: float,period_y_px: float,source: str | None=None,on_complete=None,
    ) -> bool:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.setdefault((section,plane),_GeometryCalibrationWorkflow())
        pending=(int(grid_x),int(grid_y),float(period_x_px),float(period_y_px),str(source or "").strip() or None)
        def finished(success,error,payload):
            if not success or payload is None:
                if on_complete: on_complete(False,error or RuntimeError("Geometry calibration CGH failed"))
                return
            candidate=cast(GeometryCalibrationPayload,dict(payload))
            if not self.session.upload_frame(candidate["frame"]):
                if on_complete: on_complete(False,RuntimeError("Could not upload the geometry calibration frame."))
                return
            for (workflow_section,_),other in self._workflows.items():
                if workflow_section==section:
                    other.frame_active=False
            workflow.grid_x,workflow.grid_y,workflow.period_x_px,workflow.period_y_px,workflow.detector_name=pending
            workflow.payload=candidate; workflow.measurement=None; workflow.localization=None
            workflow.localization_parameters=None; workflow.analysis=None; workflow.frame_active=True
            if on_complete: on_complete(True,None)
        return self.session.compute_geometry_orientation_cgh(
            section,grid_x=int(grid_x),grid_y=int(grid_y),period_x_px=float(period_x_px),
            period_y_px=float(period_y_px),on_finished=finished,
        )

    def acquire(self,section_key: str,observation_plane_name: str,source: str,*,on_complete=None) -> None:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None or workflow.payload is None:
            raise RuntimeError("No geometry target has been computed. Click Compute target before acquiring an image.")
        if not workflow.frame_active:
            raise RuntimeError("The geometry target is no longer displayed. Click Compute target again before acquiring an image.")
        source=str(source or "").strip()
        if not source:
            raise ValueError("Select a detector before acquisition.")
        m=self.measurements
        if m is None or not m.available:
            raise RuntimeError("Live acquisition is unavailable because no measurement provider is connected. Connect a detector or load an image from file instead.")
        old=self._requests.pop(section,None)
        if old is not None:
            try: old.cancel()
            except Exception: pass
        workflow.detector_name=source
        def success(measurement: ImageMeasurement):
            self._requests.pop(section,None)
            workflow.measurement=measurement; workflow.localization=None
            workflow.localization_parameters=None; workflow.analysis=None
            workflow.detector_name=str(measurement.detector or source).strip() or None
            if on_complete: on_complete(True,None)
        def failure(error):
            self._requests.pop(section,None)
            if on_complete: on_complete(False,error if isinstance(error,Exception) else RuntimeError(str(error)))
        request=m.acquire(section,source,metadata={
            "workflow":"geometry_orientation_calibration","observation_plane":plane,
            "slm_key":self.session.runtime.identity.key,"section_key":section,
        },on_result=success,on_error=failure)
        if request.active:
            self._requests[section]=request

    def set_measurement(self,section_key: str,observation_plane_name: str,measurement: ImageMeasurement) -> None:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None or workflow.payload is None:
            raise RuntimeError("Compute the geometry target before loading its measurement image so the expected lattice is defined.")
        workflow.measurement=measurement; workflow.localization=None
        workflow.localization_parameters=None; workflow.analysis=None

    def localize(self,section_key: str,observation_plane_name: str,parameters: Mapping[str,Any] | None) -> LocalizationResult:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None or workflow.payload is None or workflow.measurement is None:
            raise RuntimeError("Geometry localization needs both a computed target and an acquired/loaded image. Compute the target, acquire/load the image, then run localization.")
        payload=workflow.payload
        if parameters is None:
            parameters=dict(self.session.runtime.get_section_feedback_status(section).localization_params)
            parameters.update(suggest_localization_sources(
                workflow.measurement,payload.get("localization_context") or {},allow_target_hints=True,
            ))
        candidate=localize_measurement(
            workflow.measurement,target_type=str(payload["target_type"]),
            target_params=dict(payload["target_params"]),resolution=payload["resolution"],
            parameters=dict(parameters),calibration=payload.get("calibration"),
        )
        workflow.localization=candidate; workflow.localization_parameters=dict(parameters); workflow.analysis=None
        return candidate

    def analyze(self,section_key: str,observation_plane_name: str) -> GeometryOrientationAnalysis:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None or workflow.payload is None or workflow.measurement is None or workflow.localization is None:
            raise RuntimeError("No localized geometry target is available. Acquire/load the target image and run localization before Analyze.")
        analysis=analyze_geometry_orientation(
            workflow.measurement,workflow.localization,
            np.asarray(workflow.payload["target_intensities"],dtype=np.float64),
        )
        workflow.analysis=analysis
        return analysis

    def save(self,section_key: str,observation_plane_name: str) -> GeometryOrientationCalibration:
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None or workflow.analysis is None or not workflow.analysis.accepted:
            raise RuntimeError("No accepted geometry analysis is ready to save. Run Analyze and resolve any ambiguous correspondence first.")
        calibration=GeometryOrientationCalibration(
            slm_serial=self.session.runtime.identity.serial_number,section_key=section,observation_plane_name=plane,
            orientation=workflow.analysis.orientation,detector_name=workflow.detector_name,
            grid_x=workflow.grid_x,grid_y=workflow.grid_y,
            period_x_px=float(workflow.period_x_px),period_y_px=float(workflow.period_y_px),
            score=workflow.analysis.score,confidence_margin=workflow.analysis.confidence_margin,
            matched_count=workflow.analysis.matched_count,
            provenance={"measurement_id":getattr(workflow.measurement,"measurement_id",None)},
        )
        self.observation_geometry.save(calibration)
        return calibration

    def save_manual(
        self,section_key: str,observation_plane_name: str,orientation: FeedbackOrientation | str,
    ) -> GeometryOrientationCalibration:
        """Persist an explicit manual section × observation-plane geometry."""
        self._require_editor_mode(); section,plane=self._key(section_key,observation_plane_name)
        workflow=self._workflows.get((section,plane))
        if workflow is None:
            gx,gy=5,4
            px,py=self._period_guess_px(section,gx,gy)
            workflow=_GeometryCalibrationWorkflow(
                grid_x=gx,grid_y=gy,period_x_px=px,period_y_px=py,
            )
            self._workflows[(section,plane)]=workflow
        calibration=GeometryOrientationCalibration(
            slm_serial=self.session.runtime.identity.serial_number,
            section_key=section,observation_plane_name=plane,
            orientation=FeedbackOrientation.normalize(orientation),
            detector_name=workflow.detector_name,
            grid_x=int(workflow.grid_x),grid_y=int(workflow.grid_y),
            period_x_px=float(
                workflow.period_x_px
                or self._period_guess_px(section,workflow.grid_x,workflow.grid_y)[0]
            ),
            period_y_px=float(
                workflow.period_y_px
                or self._period_guess_px(section,workflow.grid_x,workflow.grid_y)[1]
            ),
            protocol="manual_orientation_v1",
            provenance={"method":"manual","source":"manual_orientation"},
        )
        self.observation_geometry.save(calibration)
        return calibration

    def finish(self,section_key: str) -> None:
        self._require_editor_mode(); section=str(section_key)
        active=self._active_frame(section)
        if active is None:
            return
        _plane,workflow=active
        if not self.session.upload_current_frame():
            raise RuntimeError("Could not restore the current experiment CGH.")
        workflow.frame_active=False

    def discard(self,section_key: str,observation_plane_name: str | None=None) -> None:
        section=str(section_key)
        if observation_plane_name is None:
            for key in [key for key in self._workflows if key[0]==section]:
                self._workflows.pop(key,None)
        else:
            self._workflows.pop((section,self._require_plane(observation_plane_name)),None)

    def cancel_all(self) -> None:
        for request in tuple(self._requests.values()):
            try: request.cancel()
            except Exception: pass
        self._requests.clear()

    def prepare_runtime_change(self) -> None:
        self.cancel_all(); self._workflows.clear()

    def dispose(self) -> None:
        self.prepare_runtime_change()


__all__=[
    "GeometryCalibrationContext","GeometryCalibrationPayload","GeometryCalibrationService",
]

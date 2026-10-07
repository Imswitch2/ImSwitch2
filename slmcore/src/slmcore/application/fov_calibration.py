"""Dedicated editable-lattice FOV mapping-calibration workflow."""

from __future__ import annotations

from dataclasses import dataclass,field,replace
import math
from typing import Any,Mapping

import numpy as np

from ..core.calibration import FOVMappingCorrection,base_mapping_signature
from ..core.cgh.execution.status import CGHResultState
from ..core.cgh.feedback import orient_localization
from ..core.cgh.localization.parameters import LOCALIZATION_PARAMS
from ..core.cgh.localization.policy import suggest_localization_sources
from ..core.cgh.localization.workflow import localize_measurement,localization_site_masks
from ..core.measurement import (
    EditablePositionReferenceGeometry,ImageMeasurement,
    editable_position_reference_geometry,editable_reference_center_px,
    editable_reference_positions,fit_center_reference_geometry,
)
from .feedback.contracts import MeasurementDispatcher


def _localization_defaults() -> dict[str,Any]:
    return {key:spec.validate(spec.default) for key,spec in LOCALIZATION_PARAMS.items()}


def _validated_parameters(values: Mapping[str,Any]) -> dict[str,Any]:
    result=_localization_defaults(); result.update(dict(values or {}))
    return {key:LOCALIZATION_PARAMS[key].validate(result[key]) for key in LOCALIZATION_PARAMS}


@dataclass
class FOVCalibrationState:
    section_key: str
    mapping_name: str
    observation_plane_name: str | None=None
    source: str | None=None
    pixel_size_um_override: float | None=None
    reference: Any=None
    measurement: ImageMeasurement | None=None
    localization: Any=None
    localization_parameters: dict[str,Any]=field(default_factory=_localization_defaults)
    editable_geometry: EditablePositionReferenceGeometry | None=None
    fit_geometry: EditablePositionReferenceGeometry | None=None
    locks: dict[str,bool]=field(default_factory=lambda:{
        "period_x_px":False,"period_y_px":False,
        "rotation_deg":False,"lattice_angle_deg":False,
    })
    candidate: FOVMappingCorrection | None=None


class FOVCalibrationService:
    """Mapping-calibration workflow independent of Position Feedback state."""

    def __init__(
        self,session,*,mappings,observation_planes,observation_geometry,
        calibration_service,startup_preferences=None,
        measurements: MeasurementDispatcher | None=None,
    ) -> None:
        self.session=session
        self.mappings=mappings
        self.observation_planes=observation_planes
        self.observation_geometry=observation_geometry
        self.calibration_service=calibration_service
        self.startup_preferences=startup_preferences
        self.measurements=measurements
        self._states: dict[str,FOVCalibrationState]={}
        self._requests={}

    def set_measurement_dispatcher(self,measurements: MeasurementDispatcher | None) -> None:
        self.measurements=measurements

    def state(self,section_key: str) -> FOVCalibrationState | None:
        return self._states.get(str(section_key))

    def ensure_context(self,section_key: str,mapping_name: str) -> FOVCalibrationState:
        section=str(section_key); mapping=str(mapping_name or "").strip()
        if not mapping: raise ValueError("mapping_name must not be empty")
        self.mappings.get(mapping)
        state=self._states.get(section)
        if state is None or state.mapping_name!=mapping:
            planes=self.observation_planes.list()
            plane=planes[0] if len(planes)==1 else None
            state=FOVCalibrationState(
                section_key=section,mapping_name=mapping,
                observation_plane_name=None if plane is None else plane.name,
                source=None if plane is None else plane.preferred_detector,
            )
            self._states[section]=state
        return state

    def discard(self,section_key: str) -> None:
        request=self._requests.pop(str(section_key),None)
        if request is not None:
            try: request.cancel()
            except Exception: pass
        self._states.pop(str(section_key),None)

    def observation_plane_names(self) -> tuple[str,...]:
        return tuple(plane.name for plane in self.observation_planes.list())

    def select_observation_plane(self,section_key: str,mapping_name: str,observation_plane_name: str | None) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        selected=str(observation_plane_name or "").strip() or None
        plane=None if selected is None else self.observation_planes.get(selected)
        if selected!=state.observation_plane_name:
            state.observation_plane_name=selected
            state.source=None if plane is None else plane.preferred_detector
            state.pixel_size_um_override=None
            self._clear_measurement(state)
        return state

    def set_source(self,section_key: str,mapping_name: str,source: str | None) -> None:
        state=self.ensure_context(section_key,mapping_name)
        value=str(source or "").strip() or None
        if value!=state.source:
            state.source=value; self._clear_measurement(state)

    def set_pixel_size_override(self,section_key: str,mapping_name: str,value: float | None) -> None:
        state=self.ensure_context(section_key,mapping_name)
        if value is None: state.pixel_size_um_override=None; return
        value=float(value)
        if not math.isfinite(value) or value<=0: raise ValueError("pixel_size_um must be positive")
        state.pixel_size_um_override=value

    def ensure_reference(self,section_key: str,mapping_name: str) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        try:
            reference=self.session.runtime.get_section_base_target_localization_reference(section_key)
        except RuntimeError as error:
            text=str(error)
            if "No CGH target selected" in text:
                raise RuntimeError(
                    "No CGH target is selected for this section. Return to the SLM section, "
                    "choose a feedback-capable CGH target, compute/apply it, then start FOV calibration."
                ) from error
            if "does not support feedback" in text:
                raise RuntimeError(
                    "The selected CGH target does not support the position feedback required for FOV calibration. "
                    "Choose a feedback-capable CGH target, compute/apply it, then retry."
                ) from error
            if "Compute the CGH" in text or "Recompute the CGH" in text:
                raise RuntimeError(
                    "The current CGH target is not ready for FOV calibration. Compute and apply the CGH "
                    "for this section, then retry."
                ) from error
            raise
        if state.reference is None or str(state.reference.target_signature)!=str(reference.target_signature):
            state.reference=reference; self._clear_measurement(state)
            params=_localization_defaults(); context=dict(reference.localization_context)
            params["period_prior_mode"]="auto"
            params["stagger_prior_mode"]="target" if context.get("target_stagger") is not None else "auto"
            params["lattice_size_prior_mode"]="target" if context.get("target_lattice_count") is not None else "auto"
            state.localization_parameters=_validated_parameters(params)
        return state

    def available_sources(self,section_key: str) -> tuple[str,...]:
        m=self.measurements
        if m is None or not m.available: return ()
        try: return tuple(str(v).strip() for v in m.available_sources(section_key) if str(v).strip())
        except Exception: return ()

    def preferred_source(self,section_key: str,mapping_name: str) -> str | None:
        state=self.ensure_context(section_key,mapping_name); sources=self.available_sources(section_key)
        if state.source in sources: return state.source
        if state.observation_plane_name:
            preferred=self.observation_planes.get(state.observation_plane_name).preferred_detector
            if preferred in sources: return preferred
        m=self.measurements
        try: value=None if m is None else m.preferred_source(section_key,sources)
        except Exception: value=None
        value=str(value or "").strip() or None
        return value if value in sources else (sources[0] if sources else None)

    def acquire(self,section_key: str,mapping_name: str,source: str | None,*,pixel_size_um: float | None=None,on_result=None,on_error=None):
        state=self.ensure_reference(section_key,mapping_name)
        if not state.observation_plane_name:
            raise RuntimeError("Select an observation plane before acquisition.")
        if pixel_size_um is not None: self.set_pixel_size_override(section_key,mapping_name,pixel_size_um)
        m=self.measurements
        if m is None or not m.available: raise RuntimeError("Live acquisition is unavailable because no measurement provider is connected. Connect a detector or load an image from file instead.")
        if self.session.runtime.get_section_cgh_status(section_key).result_state is not CGHResultState.CURRENT:
            raise RuntimeError("The current CGH target is not computed/applied. Compute and apply the CGH for this section before acquiring an FOV-calibration image.")
        selected=str(source or self.preferred_source(section_key,mapping_name) or "").strip()
        if not selected: raise RuntimeError("Select a detector/source for the active observation plane before acquiring an FOV-calibration image.")
        state.source=selected
        old=self._requests.pop(str(section_key),None)
        if old is not None:
            try: old.cancel()
            except Exception: pass
        def success(measurement):
            self._requests.pop(str(section_key),None)
            try:
                value=self.commit_measurement(section_key,mapping_name,measurement,source=selected)
                if on_result: on_result(value)
            except Exception as error:
                if on_error: on_error(error)
        def failure(error):
            self._requests.pop(str(section_key),None)
            if on_error: on_error(error if isinstance(error,Exception) else RuntimeError(str(error)))
        request=m.acquire(section_key,selected,metadata={
            "workflow":"fov_mapping_calibration","mapping_name":mapping_name,
            "observation_plane":state.observation_plane_name,"source":selected,
        },on_result=success,on_error=failure)
        if request.active: self._requests[str(section_key)]=request
        return request

    def commit_measurement(self,section_key: str,mapping_name: str,measurement: ImageMeasurement,*,source: str | None=None,pixel_size_um: float | None=None) -> FOVCalibrationState:
        state=self.ensure_reference(section_key,mapping_name)
        if not state.observation_plane_name: raise RuntimeError("Select an observation plane first.")
        if pixel_size_um is not None: self.set_pixel_size_override(section_key,mapping_name,pixel_size_um)
        scale=measurement.pixel_size_um
        # A workflow override is explicit and therefore takes precedence over
        # the observation-plane default.  Measurement metadata remains
        # authoritative when present.
        if scale is None: scale=state.pixel_size_um_override
        if scale is None:
            scale=self.observation_planes.get(state.observation_plane_name).default_pixel_size_um
        if scale is not None and measurement.pixel_size_um!=scale:
            measurement=ImageMeasurement(
                image=measurement.image,source=measurement.source,created_at=measurement.created_at,
                metadata=dict(measurement.metadata),detector=measurement.detector,
                pixel_size_um=scale,measurement_id=measurement.measurement_id,
            )
        state.source=str(source or measurement.detector or measurement.source or "").strip() or None
        state.measurement=measurement; state.localization=None; state.editable_geometry=None; state.fit_geometry=None; state.candidate=None
        parameters=_localization_defaults(); parameters.update(suggest_localization_sources(
            measurement,state.reference.localization_context,
            allow_target_hints=(measurement.source.strip().lower()!="file"),
        ))
        state.localization_parameters=_validated_parameters(parameters)
        return state

    def run_localization(self,section_key: str,mapping_name: str,parameters: Mapping[str,Any]) -> FOVCalibrationState:
        state=self.ensure_reference(section_key,mapping_name)
        if state.measurement is None: raise RuntimeError("Acquire or load an image before localization.")
        geometry=self._require_geometry(state)
        normalized=_validated_parameters(parameters)
        loc=localize_measurement(
            state.measurement,target_type=state.reference.target_type,
            target_params=state.reference.target_params,resolution=state.reference.resolution,
            parameters=normalized,calibration=None,
        )
        loc=orient_localization(loc,geometry.orientation)
        state.localization=loc; state.localization_parameters=normalized
        state.fit_geometry=editable_position_reference_geometry(loc)
        state.editable_geometry=state.fit_geometry
        state.candidate=None
        return state

    def set_editable_geometry(self,section_key: str,mapping_name: str,geometry: Mapping[str,Any],locks: Mapping[str,Any] | None=None) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        if state.localization is None: raise RuntimeError("Localize a measurement first.")
        state.editable_geometry=EditablePositionReferenceGeometry.from_mapping(geometry)
        if locks is not None:
            state.locks={key:bool(value) for key,value in dict(locks).items()}
        state.candidate=None; return state

    def square_and_straight(self,section_key: str,mapping_name: str) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        if state.editable_geometry is None: raise RuntimeError("Localize a measurement first.")
        values=state.editable_geometry.to_dict(); values.update(rotation_deg=0.0,lattice_angle_deg=90.0)
        state.editable_geometry=EditablePositionReferenceGeometry.from_mapping(values)
        state.locks["rotation_deg"]=True; state.locks["lattice_angle_deg"]=True; state.candidate=None
        return state

    def fit_center(self,section_key: str,mapping_name: str) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        if state.localization is None: raise RuntimeError("Localize a measurement first.")
        state.editable_geometry=fit_center_reference_geometry(
            state.localization,geometry=state.editable_geometry,locks=state.locks,
        ); state.candidate=None; return state

    def reset_from_fit(self,section_key: str,mapping_name: str) -> FOVCalibrationState:
        state=self.ensure_context(section_key,mapping_name)
        if state.fit_geometry is None: raise RuntimeError("Localize a measurement first.")
        state.editable_geometry=state.fit_geometry
        state.locks={key:False for key in state.locks}; state.candidate=None
        return state

    def preview(self,section_key: str,mapping_name: str) -> dict[str,Any] | None:
        state=self.ensure_context(section_key,mapping_name)
        if state.localization is None or state.editable_geometry is None: return None
        loc=state.localization; desired=editable_reference_positions(loc,state.editable_geometry)
        return {
            "cropped_image":np.asarray(loc.cropped_image,dtype=np.float64),
            "measured_positions_px":np.asarray(loc.measured_positions_px,dtype=np.float64),
            "global_positions_px":np.asarray(loc.expected_positions_px,dtype=np.float64),
            "reference_positions_px":desired,
            "reference_center_px":editable_reference_center_px(loc,state.editable_geometry),
            "fit_center_px":editable_reference_center_px(loc,state.fit_geometry or state.editable_geometry),
        }

    def fit_candidate(self,section_key: str,mapping_name: str,*,model: str="polynomial",degree: int=2) -> FOVMappingCorrection:
        state=self.ensure_context(section_key,mapping_name)
        if state.localization is None or state.editable_geometry is None or state.measurement is None:
            raise RuntimeError("Acquire/load, localize, and define the editable lattice before fitting.")
        geometry_calibration=self._require_geometry(state)
        base=self.calibration_service.saved_calibration(section_key,mapping_name)
        if not base.is_valid(): raise RuntimeError("Calibrate the base mapping before fitting an FOV correction.")
        ideal=np.asarray(self.session.runtime.get_section_fov_mapping_context(section_key)["ideal_positions_kxy"],dtype=np.float64)
        loc=state.localization
        reference_indices=np.asarray(state.reference.resolution.lattice_indices)
        localized_indices=np.asarray(loc.lattice_indices)
        if (
            ideal.shape!=loc.measured_positions_px.shape
            or reference_indices.shape!=localized_indices.shape
            or not np.array_equal(reference_indices,localized_indices)
        ):
            raise RuntimeError("Localized point identities do not match the current target.")
        desired=editable_reference_positions(loc,state.editable_geometry)
        measured=np.asarray(loc.measured_positions_px,dtype=np.float64)
        registered=np.asarray(loc.expected_positions_px,dtype=np.float64)
        matched,_inferred,_resolved=localization_site_masks(loc)
        if int(np.count_nonzero(matched)) < 3:
            raise RuntimeError(
                "FOV fitting requires at least three genuinely measured spots"
            )
        linear=_fit_affine_linear(registered,ideal)
        displacement=(linear@(desired-measured))[:,matched]
        fit_positions=ideal[:,matched]
        correction=FOVMappingCorrection.fit(
            name="Current candidate",slm_serial=self.session.runtime.identity.serial_number,
            section_key=section_key,mapping_name=mapping_name,
            base_mapping_signature=base_mapping_signature(base),
            ideal_positions_kxy=fit_positions,total_displacements_kxy=displacement,
            model=model,degree=degree,detector_name=state.measurement.detector,
            provenance={
                "workflow":"editable_lattice_fov_calibration",
                "observation_plane":state.observation_plane_name,
                "source":state.source,
                "detector":state.measurement.detector,
                "measurement_source":state.measurement.source,
                "measurement_pixel_size_um":state.measurement.pixel_size_um,
                "geometry_calibration":{
                    "orientation":geometry_calibration.orientation.value,
                    "protocol":geometry_calibration.protocol,
                    "created_at":geometry_calibration.created_at,
                },
                "desired_positions_source":"editable_lattice",
                "matched_sample_count":int(np.count_nonzero(matched)),
                "excluded_inferred_count":int(np.count_nonzero(_inferred)),
                "excluded_unresolved_count":int(np.count_nonzero(~_resolved)),
            },
        )
        state.candidate=correction; return correction

    def save_candidate(self,section_key: str,mapping_name: str,name: str,*,overwrite: bool=False,set_default: bool=False) -> FOVMappingCorrection:
        state=self.ensure_context(section_key,mapping_name)
        if state.candidate is None: raise RuntimeError("Fit an FOV correction before saving.")
        saved=state.candidate.with_name(str(name))
        self.mappings.save_fov_correction(saved,overwrite=overwrite)
        if set_default:
            if self.startup_preferences is None:
                raise RuntimeError("Default FOV preferences are unavailable.")
            current=self.calibration_service.saved_calibration(section_key,mapping_name)
            if (not current.is_valid() or saved.base_mapping_signature!=base_mapping_signature(current)):
                raise RuntimeError("Incompatible FOV corrections cannot be set as default.")
            self.startup_preferences.set_default_fov_correction(section_key,mapping_name,saved.name)
        return saved

    def _require_geometry(self,state: FOVCalibrationState):
        plane=state.observation_plane_name
        if not plane:
            raise RuntimeError("Select an observation plane first.")
        geometry=self.observation_geometry.calibration(state.section_key,plane)
        if geometry is None:
            raise RuntimeError(
                "Geometry not calibrated for Section %s × Plane %s. "
                "Open Geometry calibration from the Calibration workspace, save Manual or CGH-target geometry, then retry FOV localization."
                % (state.section_key,plane)
            )
        return geometry

    @staticmethod
    def _clear_measurement(state: FOVCalibrationState) -> None:
        state.measurement=None; state.localization=None; state.editable_geometry=None; state.fit_geometry=None; state.candidate=None


def _fit_affine_linear(source: np.ndarray,target: np.ndarray) -> np.ndarray:
    source=np.asarray(source,dtype=np.float64); target=np.asarray(target,dtype=np.float64)
    if source.shape!=target.shape or source.ndim!=2 or source.shape[0]!=2 or source.shape[1]<3:
        raise ValueError("FOV correspondence arrays must share shape (2, N) with at least three points")
    design=np.column_stack([source.T,np.ones(source.shape[1])])
    coefficients,_,rank,_=np.linalg.lstsq(design,target.T,rcond=None)
    if int(rank)<3: raise ValueError("FOV correspondence is rank-deficient")
    linear=coefficients[:2,:].T
    if abs(float(np.linalg.det(linear)))<1e-12: raise ValueError("FOV correspondence is singular")
    return linear


__all__=["FOVCalibrationService","FOVCalibrationState"]

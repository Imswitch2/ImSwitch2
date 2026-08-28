from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from typing import Any,Mapping

import numpy as np

from ..calibration import SLMSectionCalibration
from ..cgh import (
    CGHIterationMetrics,
    CGHResult,
    CGHSpec,
    CGHSessionSnapshot,
    CGHRound,
    TargetDefinitionState,
    FeedbackMeasurement,
    IntensityAdaptation,
    IntensityAnalysis,
    PositionAnalysis,
    PositionCorrection,
    RoundEvaluation,
)
from ..measurement import ImageMeasurement
from ..cgh.localization import LocalizationResult
from ..cgh.signature import CGHSignature
from ..engine.registry import SLMRegistries
from ..engine.section.context import SectionContext
from ..engine.section.geometry import SectionGeometry
from ..engine.section.model import SLMSectionState
from ..engine.section.presentation import SectionPresentation
from ..engine.device import SLMGeometry,SLMIdentity
from ..engine.corrections import ResolvedCorrections
from ..engine.state import ConfigPath,ConfigWarning


SLM_CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SLMCompiledFrame:
    """Minimal saved-config projection used for direct hardware activation."""

    identity: SLMIdentity
    geometry: SLMGeometry
    final_eightbit: np.ndarray

    def __post_init__(self) -> None:
        frame = np.asarray(self.final_eightbit)
        if frame.dtype != np.uint8:
            raise TypeError(
                "SLM compiled frame must have dtype uint8, got %s" % frame.dtype
            )
        if frame.shape != self.geometry.shape:
            raise ValueError(
                "SLM compiled frame has shape %s; expected %s"
                % (frame.shape,self.geometry.shape)
            )
        frame = np.array(frame,dtype=np.uint8,copy=True)
        frame.setflags(write=False)
        object.__setattr__(self,"final_eightbit",frame)


@dataclass
class SLMSectionConfig:
    geometry: SectionGeometry
    state: SLMSectionState
    correction_snapshot: ResolvedCorrections
    calibration: SLMSectionCalibration | None = None
    cgh_session: CGHSessionSnapshot | None = None
    presentation: SectionPresentation = field(
        default_factory=SectionPresentation,
    )

    def __post_init__(self) -> None:
        if self.correction_snapshot.geometry != self.geometry:
            raise ValueError(
                "Correction snapshot geometry does not match section geometry"
            )
        if int(self.correction_snapshot.wavelength_nm) != int(
            self.state.optics.wavelength_nm
        ):
            raise ValueError(
                "Correction snapshot wavelength does not match section optics"
            )

    def to_dict(self):
        return {
            "geometry":_section_geometry_to_dict(self.geometry),
            "state":self.state.to_dict(),
            "presentation":self.presentation.to_dict(),
            "calibration":(
                None if self.calibration is None
                else self.calibration.to_dict()
            ),
            "cgh_session":_cgh_session_snapshot_to_dict(self.cgh_session),
            "correction_snapshot":self.correction_snapshot.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str,Any],
        registries: SLMRegistries,
        *,
        path: ConfigPath=(),
    ) -> tuple['SLMSectionConfig', tuple[ConfigWarning, ...]]:
        state,warnings = SLMSectionState.from_dict(
            registries,data["state"],path=path + ("state",),
        )

        calibration_data = data.get("calibration")
        calibration = (
            None if calibration_data is None
            else SLMSectionCalibration.from_dict(calibration_data)
        )
        config = cls(
            geometry = _section_geometry_from_dict(data["geometry"]),
            state=state,
            presentation=SectionPresentation.from_dict(
                data.get("presentation"),
            ),
            calibration=calibration,
            cgh_session = _cgh_session_snapshot_from_dict(
                data.get("cgh_session")
            ),
            correction_snapshot=ResolvedCorrections.from_dict(data["correction_snapshot"]),
        )

        return config,warnings

    def clone(self,registries: SLMRegistries | None=None) -> SLMSectionConfig:
        registries = self.state.registries if registries is None else registries
        clone,warnings = type(self).from_dict(self.to_dict(),registries)

        if warnings:
            raise RuntimeError(
                f"Unexpected warnings while cloning section config: {warnings}"
            )

        return clone


@dataclass
class SLMConfig:
    schema_version: int
    identity: SLMIdentity
    geometry: SLMGeometry
    sections: dict[str, SLMSectionConfig]
    final_eightbit: np.ndarray

    def __post_init__(self) -> None:
        frame = np.asarray(self.final_eightbit)

        if frame.dtype != np.uint8:
            raise TypeError(
                f"SLM config final_eightbit must have dtype uint8, "
                f"got {frame.dtype}"
            )
        if frame.shape != self.geometry.shape:
            raise ValueError(
                f"SLM config final_eightbit has shape {frame.shape}; "
                f"expected {self.geometry.shape}"
            )

        frame = np.array(frame,dtype=np.uint8,copy=True)
        frame.setflags(write=False)
        object.__setattr__(self,"final_eightbit",frame)

    def to_dict(self):
        return {
            "schema_version":self.schema_version,
            "identity":self.identity.to_dict(),
            "geometry":self.geometry.to_dict(),
            "sections":{
                key:section.to_dict()
                for key,section in self.sections.items()
            },
            "final_eightbit":np.array(self.final_eightbit,copy=True),
        }

    @classmethod
    def from_dict(
        cls,data: Mapping[str,Any],registries: SLMRegistries,
    ) -> tuple['SLMConfig', tuple[ConfigWarning, ...]]:
        version = int(data.get(
            "schema_version",SLM_CONFIG_SCHEMA_VERSION
        ))

        if version != SLM_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported SLM config schema version {version}; "
                f"expected {SLM_CONFIG_SCHEMA_VERSION}"
            )

        warnings = []
        sections = {}

        for key,section_data in data.get("sections",{}).items():
            section,section_warnings = SLMSectionConfig.from_dict(
                section_data,registries,path=("sections",str(key)),
            )
            sections[str(key)] = section
            warnings.extend(section_warnings)

        return cls(
            schema_version=version,
            identity=SLMIdentity.from_dict(data["identity"]),
            geometry=SLMGeometry.from_dict(data["geometry"]),
            sections=sections,
            final_eightbit=np.asarray(data["final_eightbit"]),
        ),tuple(warnings)


def _section_geometry_to_dict(geometry: SectionGeometry):
    return {
        "key":geometry.key,
        "x":geometry.x,
        "y":geometry.y,
        "width":geometry.width,
        "height":geometry.height,
    }


def _section_geometry_from_dict(
    data: Mapping[str,Any],
) -> SectionGeometry:
    return SectionGeometry(
        key=str(data["key"]),
        x=int(data["x"]),
        y=int(data["y"]),
        width=int(data["width"]),
        height=int(data["height"]),
    )


def _context_to_dict(context: SectionContext):
    return {
        "geometry":_section_geometry_to_dict(context.geometry),
        "pixel_size_um":context.pixel_size_um,
        "wavelength_nm":context.wavelength_nm,
        "pupil_radius_px":context.pupil_radius_px,
        "center_offset_x_px":context.center_offset_x_px,
        "center_offset_y_px":context.center_offset_y_px,
        "calibration":(
            None if context.calibration is None
            else context.calibration.to_dict()
        ),
    }


def _context_from_dict(data: Mapping[str,Any]) -> SectionContext:
    calibration_data = data.get("calibration")

    return SectionContext(
        geometry=_section_geometry_from_dict(data["geometry"]),
        pixel_size_um=float(data["pixel_size_um"]),
        wavelength_nm=int(data["wavelength_nm"]),
        pupil_radius_px=int(data["pupil_radius_px"]),
        center_offset_x_px=int(data["center_offset_x_px"]),
        center_offset_y_px=int(data["center_offset_y_px"]),
        calibration=(
            None if calibration_data is None
            else SLMSectionCalibration.from_dict(calibration_data)
        ),
    )


def _cgh_result_to_dict(result: CGHResult | None):
    if result is None:
        return None

    return {
        "generation":result.generation,
        "target_name":result.target_name,
        "pattern":np.array(result.pattern,copy=True),
        "spec":{
            "context":_context_to_dict(result.spec.context),
            "target_type":result.spec.target_type,
            "algorithm":result.spec.algorithm,
            "target_params":deepcopy(dict(result.spec.target_params)),
            "compute_params":deepcopy(dict(result.spec.compute_params)),
            "feedback_target_signature":str(
                result.spec.feedback_target_signature
            ),
        },
        "metrics":[
            {
                "iteration":metric.iteration,
                "efficiency":metric.efficiency,
                "uniformity":metric.uniformity,
                "normalized_std":metric.normalized_std,
            }
            for metric in result.metrics
        ],
        "warnings":list(result.warnings),
        "diagnostics":deepcopy(dict(result.diagnostics)),
    }


def _cgh_result_from_dict(
    data: Mapping[str, Any] | None,
) -> CGHResult | None:
    if data is None:
        return None

    spec_data = data["spec"]
    spec = CGHSpec(
        context=_context_from_dict(spec_data["context"]),
        target_type=str(spec_data["target_type"]),
        algorithm=str(spec_data["algorithm"]),
        target_params=spec_data.get("target_params",{}),
        compute_params=spec_data.get("compute_params",{}),
        feedback_target_signature=CGHSignature(
            str(spec_data["feedback_target_signature"])
        ),
    )

    metrics = tuple(
        CGHIterationMetrics(
            iteration=metric["iteration"],
            efficiency=metric.get("efficiency"),
            uniformity=metric["uniformity"],
            normalized_std=metric["normalized_std"],
        )
        for metric in data.get("metrics",())
    )
    return CGHResult(
        generation=int(data["generation"]),
        spec=spec,
        target_name=str(data.get("target_name") or spec.target_type),
        pattern=np.asarray(data["pattern"]),
        metrics=metrics,
        warnings=tuple(data.get("warnings",())),
        diagnostics=data.get("diagnostics",{}),
    )


def _cgh_session_snapshot_to_dict(
    snapshot: CGHSessionSnapshot | None,
):
    if snapshot is None:
        return None
    return {
        "committed_target":_committed_target_to_dict(
            snapshot.committed_target
        ),
        "position_correction":_position_correction_to_dict(
            snapshot.position_correction
        ),
        "position_active":bool(snapshot.position_active),
        "position_reference_round":(
            None
            if snapshot.position_reference_round is None
            else _cgh_round_to_dict(snapshot.position_reference_round)
        ),
        "intensity_analysis_params":deepcopy(
            dict(snapshot.intensity_analysis_params)
        ),
        "rounds":{
            str(round_record.index):_cgh_round_to_dict(round_record)
            for round_record in snapshot.rounds
        },
    }


def _cgh_session_snapshot_from_dict(
    data: Mapping[str, Any] | None,
) -> CGHSessionSnapshot | None:
    if data is None:
        return None
    rounds_data = data.get("rounds",{}) or {}
    rounds = tuple(
        _cgh_round_from_dict(rounds_data[key])
        for key in sorted(rounds_data,key=lambda value:int(value))
    )
    return CGHSessionSnapshot(
        committed_target=_committed_target_from_dict(
            data.get("committed_target")
        ),
        position_correction=_position_correction_from_dict(
            data.get("position_correction")
        ),
        position_active=bool(data.get("position_active",False)),
        position_reference_round=(
            None
            if data.get("position_reference_round") is None
            else _cgh_round_from_dict(data["position_reference_round"])
        ),
        rounds=rounds,
        intensity_analysis_params=data.get("intensity_analysis_params"),
    )


def _committed_target_to_dict(
    value: TargetDefinitionState | None,
):
    if value is None:
        return None
    return {
        "target_type":value.target_type,
        "canonical_params":deepcopy(dict(value.canonical_params)),
        "target_signature":str(value.target_signature),
        "context_signature":str(value.context_signature),
    }


def _committed_target_from_dict(
    data: Mapping[str, Any] | None,
) -> TargetDefinitionState | None:
    if data is None:
        return None
    return TargetDefinitionState(
        target_type=str(data["target_type"]),
        canonical_params=data.get("canonical_params",{}),
        target_signature=CGHSignature(str(data["target_signature"])),
        context_signature=CGHSignature(str(data["context_signature"])),
    )


def _cgh_round_to_dict(value: CGHRound):
    return {
        "index":value.index,
        "created_at":value.created_at,
        "intensities":np.array(value.intensities,copy=True),
        "feedback_target_signature":str(value.feedback_target_signature),
        "result":_cgh_result_to_dict(value.result),
        "adaptation":_intensity_adaptation_to_dict(value.adaptation),
        "evaluation":_round_evaluation_to_dict(value.evaluation),
    }


def _cgh_round_from_dict(data: Mapping[str,Any]) -> CGHRound:
    result = _cgh_result_from_dict(data["result"])
    if result is None:
        raise ValueError("Persisted CGH round is missing its result")
    return CGHRound(
        index=int(data["index"]),
        created_at=str(data.get("created_at","") or ""),
        intensities=np.asarray(data["intensities"]),
        result=result,
        feedback_target_signature=CGHSignature(
            str(data["feedback_target_signature"])
        ),
        adaptation=_intensity_adaptation_from_dict(data.get("adaptation")),
        evaluation=_round_evaluation_from_dict(data.get("evaluation")),
    )


def _round_evaluation_to_dict(
    value: RoundEvaluation | None,
):
    if value is None:
        return None
    return {
        "index":value.index,
        "measurement":_measurement_to_dict(value.measurement),
        "intensity_analysis":_intensity_analysis_to_dict(
            value.intensity_analysis
        ),
    }


def _round_evaluation_from_dict(
    data: Mapping[str, Any] | None,
) -> RoundEvaluation | None:
    if data is None:
        return None
    return RoundEvaluation(
        index=int(data["index"]),
        measurement=_measurement_from_dict(data["measurement"]),
        intensity_analysis=_intensity_analysis_from_dict(
            data.get("intensity_analysis")
        ),
    )


def _intensity_adaptation_to_dict(
    value: IntensityAdaptation | None,
):
    if value is None:
        return None
    return {
        "source_round_index":value.source_round_index,
        "created_at":value.created_at,
        "previous_intensities":np.array(value.previous_intensities,copy=True),
        "adapted_intensities":np.array(value.adapted_intensities,copy=True),
    }


def _intensity_adaptation_from_dict(
    data: Mapping[str, Any] | None,
) -> IntensityAdaptation | None:
    if data is None:
        return None
    return IntensityAdaptation(
        source_round_index=int(data["source_round_index"]),
        created_at=str(data.get("created_at","") or ""),
        previous_intensities=np.asarray(data["previous_intensities"]),
        adapted_intensities=np.asarray(data["adapted_intensities"]),
    )


def _measurement_to_dict(
    value: FeedbackMeasurement,
) -> Mapping[str,Any]:
    return {
        "acquisition":_acquisition_to_dict(value.acquisition),
        "localization":_localization_to_dict(value.localization),
        "metrics":_measurement_metrics_to_dict(value.metrics),
    }


def _measurement_from_dict(
    data: Mapping[str,Any],
) -> FeedbackMeasurement:
    return FeedbackMeasurement(
        acquisition=_acquisition_from_dict(data["acquisition"]),
        localization=_localization_from_dict(data.get("localization")),
        metrics=_measurement_metrics_from_dict(data.get("metrics")),
    )


def _acquisition_to_dict(value: ImageMeasurement) -> Mapping[str,Any]:
    return {
        "image":np.array(value.image,copy=True),
        "source":value.source,
        "detector":value.detector,
        "created_at":value.created_at,
        "measurement_id":value.measurement_id,
        "metadata":deepcopy(dict(value.metadata)),
    }


def _acquisition_from_dict(data: Mapping[str,Any]) -> ImageMeasurement:
    return ImageMeasurement(
        image=np.asarray(data["image"]),
        source=str(data.get("source","unknown")),
        detector=(
            None if data.get("detector") is None
            else str(data.get("detector"))
        ),
        created_at=str(data.get("created_at","") or ""),
        measurement_id=str(data.get("measurement_id","") or ""),
        metadata=data.get("metadata",{}),
    )


def _localization_to_dict(
    value: LocalizationResult | None,
):
    if value is None:
        return None
    return {
        "target_type":value.target_type,
        "target_params":deepcopy(dict(value.target_params)),
        "parameters":deepcopy(dict(value.parameters)),
        "lattice_indices":np.array(value.lattice_indices,copy=True),
        "crop_coord":list(value.crop_coord),
        "cropped_image":np.array(value.cropped_image,copy=True),
        "expected_positions_px":np.array(value.expected_positions_px,copy=True),
        "measured_positions_px":np.array(value.measured_positions_px,copy=True),
        "period_x_px":value.period_x_px,
        "period_y_px":value.period_y_px,
        "offset_x_px":value.offset_x_px,
        "offset_y_px":value.offset_y_px,
        "reused_previous":value.reused_previous,
        "diagnostics":deepcopy(dict(value.diagnostics)),
    }


def _localization_from_dict(
    data: Mapping[str, Any] | None,
) -> LocalizationResult | None:
    if data is None:
        return None
    return LocalizationResult(
        target_type=str(data["target_type"]),
        target_params=data.get("target_params",{}),
        parameters=data.get("parameters",{}),
        lattice_indices=np.asarray(data["lattice_indices"]),
        crop_coord=tuple(data["crop_coord"]),
        cropped_image=np.asarray(data["cropped_image"]),
        expected_positions_px=np.asarray(data["expected_positions_px"]),
        measured_positions_px=np.asarray(data["measured_positions_px"]),
        period_x_px=float(data["period_x_px"]),
        period_y_px=float(data["period_y_px"]),
        offset_x_px=float(data["offset_x_px"]),
        offset_y_px=float(data["offset_y_px"]),
        reused_previous=bool(data.get("reused_previous",False)),
        diagnostics=data.get("diagnostics",{}),
    )


def _measurement_metrics_to_dict(value):
    if value is None:
        return None
    return {
        "geometry_type":value.geometry_type,
        "values":deepcopy(dict(value.values)),
        "matched_count":value.matched_count,
        "total_count":value.total_count,
    }


def _measurement_metrics_from_dict(data):
    if data is None:
        return None
    from ..cgh.measurement_metrics import MeasurementMetrics
    return MeasurementMetrics(
        geometry_type=str(data.get("geometry_type","unknown")),
        values=data.get("values",{}),
        matched_count=int(data.get("matched_count",0)),
        total_count=int(data.get("total_count",0)),
    )


def _intensity_analysis_to_dict(
    value: IntensityAnalysis | None,
):
    if value is None:
        return None
    return {
        "geometry_type":value.geometry_type,
        "parameters":deepcopy(dict(value.parameters)),
        "spot_powers":np.array(value.spot_powers,copy=True),
        "efficiency":value.efficiency,
        "uniformity":value.uniformity,
        "normalized_std":value.normalized_std,
        "integration_preview":np.array(value.integration_preview,copy=True),
        "matched_count":value.matched_count,
        "total_count":value.total_count,
    }


def _intensity_analysis_from_dict(
    data: Mapping[str, Any] | None,
) -> IntensityAnalysis | None:
    if data is None:
        return None
    return IntensityAnalysis(
        geometry_type=str(data.get("geometry_type","lattice")),
        parameters=data.get("parameters",{}),
        spot_powers=np.asarray(data["spot_powers"]),
        efficiency=float(data["efficiency"]),
        uniformity=float(data["uniformity"]),
        normalized_std=float(data["normalized_std"]),
        integration_preview=np.asarray(data["integration_preview"]),
        matched_count=int(data.get("matched_count",len(data["spot_powers"]))),
        total_count=int(data.get("total_count",len(data["spot_powers"]))),
    )


def _position_analysis_to_dict(
    value: PositionAnalysis,
) -> Mapping[str,Any]:
    return {
        "parameters":deepcopy(dict(value.parameters)),
        "position_errors_px":np.array(value.position_errors_px,copy=True),
        "position_errors_um":(
            None if value.position_errors_um is None
            else np.array(value.position_errors_um,copy=True)
        ),
        "correction_kxy":np.array(value.correction_kxy,copy=True),
        "corrected_positions_kxy":np.array(
            value.corrected_positions_kxy,copy=True,
        ),
    }


def _position_analysis_from_dict(
    data: Mapping[str,Any],
) -> PositionAnalysis:
    return PositionAnalysis(
        parameters=data.get("parameters",{}),
        position_errors_px=np.asarray(data["position_errors_px"]),
        position_errors_um=(
            None if data.get("position_errors_um") is None
            else np.asarray(data["position_errors_um"])
        ),
        correction_kxy=np.asarray(data["correction_kxy"]),
        corrected_positions_kxy=np.asarray(data["corrected_positions_kxy"]),
    )


def _position_correction_to_dict(
    value: PositionCorrection | None,
):
    if value is None:
        return None
    return {
        "created_at":value.created_at,
        "measurement":_measurement_to_dict(value.measurement),
        "analysis":_position_analysis_to_dict(value.analysis),
        "lattice_indices":np.array(value.lattice_indices,copy=True),
        "ideal_positions_kxy":np.array(value.ideal_positions_kxy,copy=True),
        "displacement_kxy":np.array(value.displacement_kxy,copy=True),
        "corrected_positions_kxy":np.array(
            value.corrected_positions_kxy,copy=True,
        ),
        "calibration":deepcopy(dict(value.calibration)),
    }


def _position_correction_from_dict(
    data: Mapping[str, Any] | None,
) -> PositionCorrection | None:
    if data is None:
        return None
    return PositionCorrection(
        created_at=str(data.get("created_at","") or ""),
        measurement=_measurement_from_dict(data["measurement"]),
        analysis=_position_analysis_from_dict(data["analysis"]),
        lattice_indices=np.asarray(data["lattice_indices"]),
        ideal_positions_kxy=np.asarray(data["ideal_positions_kxy"]),
        displacement_kxy=np.asarray(data["displacement_kxy"]),
        corrected_positions_kxy=np.asarray(data["corrected_positions_kxy"]),
        calibration=data.get("calibration",{}),
    )

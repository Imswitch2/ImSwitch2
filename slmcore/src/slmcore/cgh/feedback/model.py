"""Typed runtime records for CGH measurement, feedback and position correction."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ...measurement import ImageMeasurement
from ..localization.model import LocalizationResult
from ..measurement_metrics import IntensityAnalysis,MeasurementMetrics


class FeedbackCapability(str,Enum):
    """Feedback/correction operations supported by one target registration."""

    INTENSITY = "intensity"
    POSITION_CORRECTION = "position_correction"


class FeedbackChangeKind(str,Enum):
    """Feedback change that created the current pending working round."""

    INTENSITY = "intensity"
    POSITION = "position"


def _freeze_array(value: Any,name: str,ndim: int | None=None,dtype=None):
    array = np.asarray(value,dtype=dtype)
    if ndim is not None and array.ndim != ndim:
        raise ValueError(f"{name} must be {ndim}D, got shape {array.shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str,Any]:
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError(f"Expected a mapping, got {type(value).__name__}")
    return MappingProxyType(deepcopy(dict(value)))


def _timestamp(value: str) -> str:
    return str(value or datetime.now().isoformat())



@dataclass(frozen=True)
class FeedbackMeasurement:
    """Current acquisition plus optional localization and derived metrics."""

    acquisition: ImageMeasurement
    localization: LocalizationResult | None = None
    metrics: MeasurementMetrics | None = None


@dataclass(frozen=True)
class RoundEvaluation:
    """Measurement, localization and analysis belonging to one computed round."""

    index: int
    measurement: FeedbackMeasurement
    intensity_analysis: IntensityAnalysis | None = None

    def __post_init__(self) -> None:
        index = int(self.index)
        if index < 0:
            raise ValueError("Round evaluation index must be >= 0")
        if not isinstance(self.measurement,FeedbackMeasurement):
            raise TypeError("measurement must be FeedbackMeasurement")
        analysis = self.intensity_analysis
        if analysis is not None and not isinstance(analysis,IntensityAnalysis):
            raise TypeError("intensity_analysis must be IntensityAnalysis or None")
        object.__setattr__(self,"index",index)

    @property
    def analysis(self) -> IntensityAnalysis | None:
        """Compatibility alias for existing inspection widgets."""
        return self.intensity_analysis


@dataclass(frozen=True)
class IntensityAdaptation:
    """Intensity transition derived from a measured source round."""

    source_round_index: int
    previous_intensities: np.ndarray
    adapted_intensities: np.ndarray
    created_at: str = ""

    def __post_init__(self) -> None:
        index = int(self.source_round_index)
        if index < 0:
            raise ValueError("source_round_index must be >= 0")
        previous = _freeze_array(
            self.previous_intensities,"previous_intensities",ndim=1,
            dtype=np.float64,
        )
        adapted = _freeze_array(
            self.adapted_intensities,"adapted_intensities",ndim=1,
            dtype=np.float64,
        )
        if previous.shape != adapted.shape:
            raise ValueError("Intensity adaptation arrays must have matching shapes")
        object.__setattr__(self,"source_round_index",index)
        object.__setattr__(self,"previous_intensities",previous)
        object.__setattr__(self,"adapted_intensities",adapted)
        object.__setattr__(self,"created_at",_timestamp(self.created_at))


@dataclass(frozen=True)
class PositionAnalysis:
    """Position-specific correction derived from one shared localization."""

    parameters: Mapping[str,Any]
    position_errors_px: np.ndarray
    position_errors_um: np.ndarray | None
    correction_kxy: np.ndarray
    corrected_positions_kxy: np.ndarray

    def __post_init__(self) -> None:
        error_px = _freeze_array(
            self.position_errors_px,"position_errors_px",ndim=2,dtype=np.float64,
        )
        error_um =(
            None if self.position_errors_um is None
            else _freeze_array(
                self.position_errors_um,"position_errors_um",
                ndim=2,dtype=np.float64,
            )
        )
        correction = _freeze_array(
            self.correction_kxy,"correction_kxy",ndim=2,dtype=np.float64,
        )
        corrected = _freeze_array(
            self.corrected_positions_kxy,"corrected_positions_kxy",ndim=2,
            dtype=np.float64,
        )
        shapes = {error_px.shape,correction.shape,corrected.shape}
        if error_um is not None:
            shapes.add(error_um.shape)
        if len(shapes) != 1 or error_px.shape[0] != 2:
            raise ValueError("Position-analysis arrays must share shape (2, N)")
        object.__setattr__(self,"parameters",_freeze_mapping(self.parameters))
        object.__setattr__(self,"position_errors_px",error_px)
        object.__setattr__(self,"position_errors_um",error_um)
        object.__setattr__(self,"correction_kxy",correction)
        object.__setattr__(self,"corrected_positions_kxy",corrected)


@dataclass(frozen=True)
class PositionCorrection:
    """Serializable-in-principle one-shot mapping sampled at ideal spot positions."""

    measurement: FeedbackMeasurement
    analysis: PositionAnalysis
    lattice_indices: np.ndarray
    ideal_positions_kxy: np.ndarray
    displacement_kxy: np.ndarray
    corrected_positions_kxy: np.ndarray
    calibration: Mapping[str,Any]
    created_at: str = ""

    def __post_init__(self) -> None:
        indices = _freeze_array(self.lattice_indices,"lattice_indices",ndim=2)
        ideal = _freeze_array(
            self.ideal_positions_kxy,"ideal_positions_kxy",ndim=2,dtype=np.float64,
        )
        displacement = _freeze_array(
            self.displacement_kxy,"displacement_kxy",ndim=2,dtype=np.float64,
        )
        corrected = _freeze_array(
            self.corrected_positions_kxy,"corrected_positions_kxy",ndim=2,
            dtype=np.float64,
        )
        if indices.shape[0] != 2:
            raise ValueError("lattice_indices must have shape (2, N)")
        if ideal.shape != displacement.shape or ideal.shape != corrected.shape:
            raise ValueError("Position-correction arrays must share shape (2, N)")
        if ideal.shape[1] != indices.shape[1]:
            raise ValueError("Position correction spot count mismatch")
        object.__setattr__(self,"lattice_indices",indices)
        object.__setattr__(self,"ideal_positions_kxy",ideal)
        object.__setattr__(self,"displacement_kxy",displacement)
        object.__setattr__(self,"corrected_positions_kxy",corrected)
        object.__setattr__(self,"calibration",_freeze_mapping(self.calibration))
        object.__setattr__(self,"created_at",_timestamp(self.created_at))

    def to_dict(self) -> Mapping[str,Any]:
        """Return a detached serialization-ready representation."""
        return {
            "created_at":self.created_at,
            "lattice_indices":self.lattice_indices.tolist(),
            "ideal_positions_kxy":self.ideal_positions_kxy.tolist(),
            "displacement_kxy":self.displacement_kxy.tolist(),
            "corrected_positions_kxy":self.corrected_positions_kxy.tolist(),
            "calibration":deepcopy(dict(self.calibration)),
            "position_parameters":deepcopy(dict(self.analysis.parameters)),
            "localization_parameters":deepcopy(
                dict(self.measurement.localization.parameters)
                if self.measurement.localization is not None else {}
            ),
        }


@dataclass(frozen=True)
class FeedbackInspection:
    """Compatibility view over session-owned feedback/round data."""

    measurement: FeedbackMeasurement | None
    intensity_rounds: tuple[RoundEvaluation, ...]
    position_correction: PositionCorrection | None
    position_active: bool

    def __post_init__(self) -> None:
        evaluations = tuple(self.intensity_rounds or ())
        for item in evaluations:
            if not isinstance(item,RoundEvaluation):
                raise TypeError(
                    "intensity_rounds must contain RoundEvaluation records"
                )
        correction = self.position_correction
        if correction is not None and not isinstance(correction,PositionCorrection):
            raise TypeError(
                "position_correction must be PositionCorrection or None"
            )
        object.__setattr__(self,"intensity_rounds",evaluations)
        object.__setattr__(self,"position_active",bool(self.position_active))


def base_cgh_recompute_would_discard_feedback(status: Any) -> bool:
    """Return whether replacing the base CGH would discard feedback state.

    This is the shared semantic predicate used by manual-compute confirmation
    and Qt auto-recompute availability. Measurement/inspection-only state does
    not block recompute; committed intensity/position/adaptation state does.
    """
    if status is None:
        return False
    return bool(
        int(getattr(status,"intensity_count",0) or 0) > 0
        or bool(getattr(status,"position_available",False))
        or bool(getattr(status,"feedback_compute_pending",False))
        or bool(getattr(status,"adaptation_pending",False))
    )


@dataclass(frozen=True)
class FeedbackStatus:
    """Small UI-facing snapshot of the current feedback session."""

    capabilities: tuple[FeedbackCapability, ...]
    acquisition_available: bool
    localization_available: bool
    intensity_count: int
    position_available: bool
    position_active: bool
    inspection_available: bool
    localization_params: Mapping[str,Any]
    intensity_params: Mapping[str,Any]
    position_params: Mapping[str,Any]
    measurement_metrics: Mapping[str,float] = field(default_factory=dict)
    adaptation_pending: bool = False
    feedback_compute_pending: bool = False
    pending_feedback_change: FeedbackChangeKind | None = None

    # Localization workflow/summary fields. These are intentionally status
    # metadata rather than localization parameters.
    previous_localization_available: bool = False
    localization_matched_count: int = 0
    localization_total_count: int = 0
    localization_missing_count: int = 0
    localization_unmatched_detection_count: int = 0
    localization_rms_residual_px: float | None = None
    localization_reused_previous: bool = False

    def __post_init__(self) -> None:
        capabilities = tuple(
            FeedbackCapability(item) for item in self.capabilities
        )
        object.__setattr__(self,"capabilities",capabilities)
        object.__setattr__(
            self,"acquisition_available",bool(self.acquisition_available),
        )
        object.__setattr__(
            self,"localization_available",bool(self.localization_available),
        )
        object.__setattr__(self,"intensity_count",int(self.intensity_count))
        object.__setattr__(
            self,"position_available",bool(self.position_available),
        )
        object.__setattr__(self,"position_active",bool(self.position_active))
        object.__setattr__(
            self,"inspection_available",bool(self.inspection_available),
        )
        object.__setattr__(
            self,"localization_params",_freeze_mapping(self.localization_params),
        )
        object.__setattr__(
            self,"intensity_params",_freeze_mapping(self.intensity_params),
        )
        object.__setattr__(
            self,"position_params",_freeze_mapping(self.position_params),
        )
        object.__setattr__(
            self,"measurement_metrics",_freeze_mapping(self.measurement_metrics),
        )
        object.__setattr__(
            self,"adaptation_pending",bool(self.adaptation_pending),
        )
        object.__setattr__(
            self,"feedback_compute_pending",bool(self.feedback_compute_pending),
        )
        pending_change = self.pending_feedback_change
        object.__setattr__(
            self,
            "pending_feedback_change",
            None if pending_change is None else FeedbackChangeKind(pending_change),
        )
        object.__setattr__(
            self,"previous_localization_available",
            bool(self.previous_localization_available),
        )
        object.__setattr__(
            self,"localization_matched_count",
            int(self.localization_matched_count),
        )
        object.__setattr__(
            self,"localization_total_count",
            int(self.localization_total_count),
        )
        object.__setattr__(
            self,"localization_missing_count",
            int(self.localization_missing_count),
        )
        object.__setattr__(
            self,"localization_unmatched_detection_count",
            int(self.localization_unmatched_detection_count),
        )
        rms = self.localization_rms_residual_px
        object.__setattr__(
            self,
            "localization_rms_residual_px",
            None if rms is None else float(rms),
        )
        object.__setattr__(
            self,"localization_reused_previous",
            bool(self.localization_reused_previous),
        )

    def supports(self,capability: FeedbackCapability) -> bool:
        return FeedbackCapability(capability) in self.capabilities

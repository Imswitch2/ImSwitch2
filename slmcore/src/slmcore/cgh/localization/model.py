"""Immutable results and options for reusable image spot localization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..lattice_geometry import LatticeRepresentation


def _freeze_array(value,name,ndim=None,dtype=None):
    array = np.asarray(value,dtype=dtype)
    if ndim is not None and array.ndim != ndim:
        raise ValueError("%s must be %dD, got shape %s" % (name,ndim,array.shape))
    if not np.all(np.isfinite(array)):
        raise ValueError("%s contains non-finite values" % name)
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


def _freeze_mapping(value):
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError("Expected a mapping, got %s" % type(value).__name__)
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class SpotDetectionOptions:
    """Numerical options for target-independent spot detection."""

    crop_threshold: float = 0.4
    dilation_kernel_size: int = 3
    blur_sigma: float = 1.0
    threshold_rel: float = 0.10
    min_distance_px: float | None = None
    min_distance_fraction: float = 0.35
    refinement_method: str = "cog"
    refinement_window_px: int = 2
    max_spots: int | None = None

    def __post_init__(self):
        if not 0.0 <= float(self.crop_threshold) <= 1.0:
            raise ValueError("crop_threshold must be in [0, 1]")
        if int(self.dilation_kernel_size) < 1:
            raise ValueError("dilation_kernel_size must be >= 1")
        if float(self.blur_sigma) < 0.0:
            raise ValueError("blur_sigma must be >= 0")
        if not 0.0 <= float(self.threshold_rel) <= 1.0:
            raise ValueError("threshold_rel must be in [0, 1]")
        if self.min_distance_px is not None and float(self.min_distance_px) <= 0:
            raise ValueError("min_distance_px must be > 0 when provided")
        if float(self.min_distance_fraction) <= 0:
            raise ValueError("min_distance_fraction must be > 0")
        if str(self.refinement_method) not in ("max","cog"):
            raise ValueError("refinement_method must be 'max' or 'cog'")
        if int(self.refinement_window_px) < 0:
            raise ValueError("refinement_window_px must be >= 0")
        if self.max_spots is not None and int(self.max_spots) <= 0:
            raise ValueError("max_spots must be > 0 when provided")


@dataclass(frozen=True)
class LatticeRegistrationOptions:
    """Options for global affine lattice registration.

    ``expected_period_px`` guides the FFT candidate search. A scalar means
    approximately equal primitive periods; a two-tuple supplies separate expected
    primitive lengths. The final affine fit remains free and registration can
    fall back to an unconstrained global search. Lattice orientation remains free.
    """

    expected_period_px: float | tuple[float, float] | None = None
    period_tolerance_fraction: float = 0.25
    fft_exclude_fraction: float = 0.03
    fft_peak_count: int = 14
    lattice_candidate_search_mode: str = "fast"
    matching_gate_fraction: float = 0.40
    robust_outlier_sigma: float = 3.5
    max_iterations: int = 6
    min_match_fraction: float = 0.35
    expected_rotation_deg: float | None = None

    def __post_init__(self):
        period = self.expected_period_px
        if period is not None:
            if np.isscalar(period):
                value = float(period)
                period = (value,value)
            else:
                if len(period) != 2:
                    raise ValueError("expected_period_px must be a scalar or two values")
                period = (float(period[0]),float(period[1]))
            if period[0] <= 0 or period[1] <= 0:
                raise ValueError("expected periods must be > 0")
            object.__setattr__(self,"expected_period_px",period)
        if float(self.period_tolerance_fraction) < 0:
            raise ValueError("period_tolerance_fraction must be >= 0")
        if not 0.0 <= float(self.fft_exclude_fraction) < 0.5:
            raise ValueError("fft_exclude_fraction must be in [0, 0.5)")
        if int(self.fft_peak_count) < 2:
            raise ValueError("fft_peak_count must be >= 2")
        if str(self.lattice_candidate_search_mode) not in ("fast","full"):
            raise ValueError(
                "lattice_candidate_search_mode must be 'fast' or 'full'"
            )
        if float(self.matching_gate_fraction) <= 0:
            raise ValueError("matching_gate_fraction must be > 0")
        if float(self.robust_outlier_sigma) <= 0:
            raise ValueError("robust_outlier_sigma must be > 0")
        if int(self.max_iterations) < 1:
            raise ValueError("max_iterations must be >= 1")
        if not 0.0 < float(self.min_match_fraction) <= 1.0:
            raise ValueError("min_match_fraction must be in (0, 1]")


@dataclass(frozen=True)
class DetectedSpots:
    """Target-independent, unordered subpixel spot detections."""

    positions_px: np.ndarray
    intensities: np.ndarray
    scores: np.ndarray
    cropped_image: np.ndarray
    processed_image: np.ndarray
    crop_coord: tuple[int, int, int, int]
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self):
        positions = _freeze_array(self.positions_px,"positions_px",ndim=2,dtype=np.float64)
        if positions.shape[0] != 2:
            raise ValueError("positions_px must have shape (2, N)")
        intensities = _freeze_array(self.intensities,"intensities",ndim=1,dtype=np.float64)
        scores = _freeze_array(self.scores,"scores",ndim=1,dtype=np.float64)
        if intensities.shape != (positions.shape[1],) or scores.shape != intensities.shape:
            raise ValueError("detection arrays must align")
        crop = tuple(int(v) for v in self.crop_coord)
        if len(crop) != 4:
            raise ValueError("crop_coord must be (y1, y2, x1, x2)")
        object.__setattr__(self,"positions_px",positions)
        object.__setattr__(self,"intensities",intensities)
        object.__setattr__(self,"scores",scores)
        object.__setattr__(self,"cropped_image",_freeze_array(
            self.cropped_image,"cropped_image",ndim=2,dtype=np.float64,
        ))
        object.__setattr__(self,"processed_image",_freeze_array(
            self.processed_image,"processed_image",ndim=2,dtype=np.float64,
        ))
        object.__setattr__(self,"crop_coord",crop)
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))


@dataclass(frozen=True)
class LatticeModel:
    """Indexed logical lattice prior to its detector-space affine transform.

    ``representation`` retains the finite translation-cell + motif structure.
    ``logical_positions`` is the centered numerical projection used by the
    affine registration solver.
    """

    lattice_indices: np.ndarray
    logical_positions: np.ndarray
    representation: LatticeRepresentation | None = None
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self):
        indices = _freeze_array(self.lattice_indices,"lattice_indices",ndim=2)
        logical = _freeze_array(self.logical_positions,"logical_positions",ndim=2,dtype=np.float64)
        if indices.shape[0] != 2 or logical.shape != indices.shape:
            raise ValueError("lattice_indices/logical_positions must share shape (2, N)")
        representation = self.representation
        if representation is not None:
            if not isinstance(representation,LatticeRepresentation):
                raise TypeError("representation must be a LatticeRepresentation")
            if not np.array_equal(representation.lattice_indices,indices):
                raise ValueError("representation lattice indices do not match model")
        object.__setattr__(self,"lattice_indices",indices)
        object.__setattr__(self,"logical_positions",logical)
        object.__setattr__(self,"representation",representation)
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))


@dataclass(frozen=True)
class LatticeRegistration:
    """Robust mapping from indexed lattice points to unordered detections."""

    model: LatticeModel
    detections: DetectedSpots
    affine_linear: np.ndarray
    affine_translation: np.ndarray
    expected_positions_px: np.ndarray
    measured_positions_px: np.ndarray
    matched_mask: np.ndarray
    detection_indices: np.ndarray
    residuals_px: np.ndarray
    rms_residual_px: float
    reused_previous: bool = False
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self):
        linear = _freeze_array(self.affine_linear,"affine_linear",ndim=2,dtype=np.float64)
        translation = _freeze_array(self.affine_translation,"affine_translation",ndim=1,dtype=np.float64)
        if linear.shape != (2,2) or translation.shape != (2,):
            raise ValueError("affine transform must be 2x2 + length-2 translation")
        expected = _freeze_array(self.expected_positions_px,"expected_positions_px",ndim=2,dtype=np.float64)
        measured = _freeze_array(self.measured_positions_px,"measured_positions_px",ndim=2,dtype=np.float64)
        residuals = _freeze_array(self.residuals_px,"residuals_px",ndim=2,dtype=np.float64)
        if expected.shape != self.model.logical_positions.shape:
            raise ValueError("expected positions must align with lattice model")
        if measured.shape != expected.shape or residuals.shape != expected.shape:
            raise ValueError("registration arrays must share shape (2, N)")
        matched = np.asarray(self.matched_mask,dtype=bool)
        detection_indices = np.asarray(self.detection_indices,dtype=np.int64)
        if matched.shape != (expected.shape[1],) or detection_indices.shape != matched.shape:
            raise ValueError("matched_mask/detection_indices must have shape (N,)")
        if np.any(detection_indices[matched] < 0):
            raise ValueError("matched lattice points require non-negative detection indices")
        matched = np.array(matched,copy=True); matched.setflags(write=False)
        detection_indices = np.array(detection_indices,copy=True); detection_indices.setflags(write=False)
        object.__setattr__(self,"affine_linear",linear)
        object.__setattr__(self,"affine_translation",translation)
        object.__setattr__(self,"expected_positions_px",expected)
        object.__setattr__(self,"measured_positions_px",measured)
        object.__setattr__(self,"matched_mask",matched)
        object.__setattr__(self,"detection_indices",detection_indices)
        object.__setattr__(self,"residuals_px",residuals)
        object.__setattr__(self,"rms_residual_px",float(self.rms_residual_px))
        object.__setattr__(self,"reused_previous",bool(self.reused_previous))
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))

    @property
    def matched_count(self):
        return int(np.count_nonzero(self.matched_mask))

    @property
    def missing_lattice_indices(self):
        return self.model.lattice_indices[:,~self.matched_mask]

    @property
    def unmatched_detection_indices(self):
        used = set(int(v) for v in self.detection_indices[self.matched_mask])
        return tuple(i for i in range(self.detections.positions_px.shape[1]) if i not in used)

    @property
    def basis_vectors_px(self):
        return np.array(self.affine_linear,copy=True)

    @property
    def period_x_px(self):
        return float(np.linalg.norm(self.affine_linear[:,0]))

    @property
    def period_y_px(self):
        return float(np.linalg.norm(self.affine_linear[:,1]))

    @property
    def rotation_deg(self):
        a = self.affine_linear[:,0]
        return float(np.degrees(np.arctan2(a[1],a[0])))

    @property
    def lattice_angle_deg(self):
        a = self.affine_linear[:,0]
        b = self.affine_linear[:,1]
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom <= 0:
            return float("nan")
        cosine = float(np.clip(np.dot(a,b) / denom,-1.0,1.0))
        return float(np.degrees(np.arccos(cosine)))

@dataclass(frozen=True)
class LocalizationResult:
    """Target-aware localization result reusable across host workflows.

    The low-level detector/lattice registration remains target-independent.
    This record adds the target correspondence, accepted parameters and image
    crop needed by feedback, calibration and inspection workflows.
    """

    target_type: str
    target_params: Mapping[str,Any]
    parameters: Mapping[str,Any]
    lattice_indices: np.ndarray
    crop_coord: tuple[int, int, int, int]
    cropped_image: np.ndarray
    expected_positions_px: np.ndarray
    measured_positions_px: np.ndarray
    period_x_px: float
    period_y_px: float
    offset_x_px: float
    offset_y_px: float
    reused_previous: bool = False
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self):
        lattice_indices = _freeze_array(
            self.lattice_indices,"lattice_indices",ndim=2,
        )
        expected = _freeze_array(
            self.expected_positions_px,"expected_positions_px",ndim=2,
            dtype=np.float64,
        )
        measured = _freeze_array(
            self.measured_positions_px,"measured_positions_px",ndim=2,
            dtype=np.float64,
        )
        if lattice_indices.shape[0] != 2:
            raise ValueError("lattice_indices must have shape (2, N)")
        if expected.shape != measured.shape or expected.shape[0] != 2:
            raise ValueError(
                "expected_positions_px and measured_positions_px must share "
                "shape (2, N)"
            )
        if expected.shape[1] != lattice_indices.shape[1]:
            raise ValueError("Localization spot count does not match lattice indices")
        crop = tuple(int(value) for value in self.crop_coord)
        if len(crop) != 4:
            raise ValueError("crop_coord must be (y1, y2, x1, x2)")

        object.__setattr__(self,"target_type",str(self.target_type))
        object.__setattr__(self,"target_params",_freeze_mapping(self.target_params))
        object.__setattr__(self,"parameters",_freeze_mapping(self.parameters))
        object.__setattr__(self,"lattice_indices",lattice_indices)
        object.__setattr__(self,"crop_coord",crop)
        object.__setattr__(
            self,"cropped_image",
            _freeze_array(self.cropped_image,"cropped_image",ndim=2,dtype=np.float64),
        )
        object.__setattr__(self,"expected_positions_px",expected)
        object.__setattr__(self,"measured_positions_px",measured)
        object.__setattr__(self,"period_x_px",float(self.period_x_px))
        object.__setattr__(self,"period_y_px",float(self.period_y_px))
        object.__setattr__(self,"offset_x_px",float(self.offset_x_px))
        object.__setattr__(self,"offset_y_px",float(self.offset_y_px))
        object.__setattr__(self,"reused_previous",bool(self.reused_previous))
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))

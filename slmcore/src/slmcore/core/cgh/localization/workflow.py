"""High-level image-measurement localization workflow.

The workflow combines a generic :class:`ImageMeasurement`, resolved geometric
guidance, spot detection and lattice registration into a stable
:class:`LocalizationResult`.  It contains no detector acquisition or UI logic;
host applications provide measurements and decide when results are committed.
"""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np

from ...measurement import ImageMeasurement
from ..targets.resolution import TargetResolution
from .api import localize_lattice
from .guidance import (
    _target_lattice_size_hint,
    _validated_lattice_indices,
    resolve_localization_guidance,
)
from .lattice import rectangular_lattice_indices
from .model import (
    LatticeRegistrationOptions,
    LocalizationResult,
    SpotDetectionOptions,
)

def localize_measurement(
    measurement: ImageMeasurement,
    *,
    target_type: str,
    target_params: Mapping[str,Any],
    resolution: TargetResolution,
    parameters: Mapping[str,Any],
    calibration: Any=None,
) -> LocalizationResult:
    """Detect spots and register one target lattice to one image measurement."""
    if not isinstance(measurement,ImageMeasurement):
        raise TypeError("measurement must be an ImageMeasurement")
    lattice_indices = _validated_lattice_indices(resolution)
    if lattice_indices.shape[1] < 3:
        raise ValueError("Target localization requires at least three target spots")

    hints = resolve_localization_guidance(
        target_params=target_params,
        resolution=resolution,
        calibration=calibration,
        parameters=parameters,
    )
    if hints.geometry_type != "lattice":
        raise ValueError(
            "Unsupported localization geometry type '%s'" % hints.geometry_type
        )

    if hints.count_x is None or hints.count_y is None:
        localization_indices = None
    elif (
        hints.count_source == "target"
        and _target_lattice_size_hint(resolution)
            == (hints.count_x,hints.count_y)
    ):
        localization_indices = lattice_indices
    else:
        localization_indices = rectangular_lattice_indices(
            hints.count_x,hints.count_y,
        )

    detection_options = SpotDetectionOptions(
        crop_threshold=float(parameters["crop_threshold"]),
        dilation_kernel_size=int(parameters["dilation_kernel_size"]),
        blur_sigma=float(parameters["spot_blur_sigma"]),
        threshold_rel=float(parameters["spot_threshold_rel"]),
        min_distance_fraction=float(parameters["spot_min_distance_fraction"]),
        refinement_method=str(parameters["focus_localization_method"]),
        refinement_window_px=int(parameters["focus_search_window_px"]),
    )
    registration_options = LatticeRegistrationOptions(
        expected_period_px=hints.expected_period_px,
        period_tolerance_fraction=float(parameters["period_tolerance_fraction"]),
        fft_exclude_fraction=float(parameters["fft_exclude_fraction"]),
        fft_peak_count=int(parameters["fft_peak_count"]),
        lattice_candidate_search_mode=str(
            parameters["lattice_candidate_search_mode"]
        ),
        matching_gate_fraction=float(parameters["matching_gate_fraction"]),
        robust_outlier_sigma=float(parameters["robust_outlier_sigma"]),
        max_iterations=int(parameters["max_registration_iterations"]),
        min_match_fraction=float(parameters["min_match_fraction"]),
        expected_rotation_deg=None,
    )

    registration = localize_lattice(
        np.asarray(measurement.image,dtype=np.float64),
        localization_indices,
        stagger=hints.stagger,
        detection_options=detection_options,
        registration_options=registration_options,
    )

    linear = np.asarray(registration.affine_linear,dtype=np.float64)
    translation = np.asarray(registration.affine_translation,dtype=np.float64)
    periods = np.linalg.norm(linear,axis=0)
    diagnostics = {
        "acquisition_source":measurement.source,
        "acquisition_created_at":measurement.created_at,
        "measurement_source":measurement.source,
        "measurement_created_at":measurement.created_at,
        "measurement_id":measurement.measurement_id,
        "measurement_detector":measurement.detector,
        "pattern_geometry_type":hints.geometry_type,
        "period_prior_source":hints.period_source,
        "stagger_prior_source":hints.stagger_source,
        "lattice_size_prior_source":hints.count_source,
        "resolved_stagger":registration.diagnostics.get(
            "resolved_stagger",hints.stagger
        ),
        "stagger_resolution_source":registration.diagnostics.get(
            "stagger_source",hints.stagger_source
        ),
        "canonical_stagger":registration.diagnostics.get("canonical_stagger"),
        "expected_period_px":(
            None if hints.expected_period_px is None
            else tuple(float(value) for value in hints.expected_period_px)
        ),
        "resolved_lattice_count":(
            int(np.unique(registration.model.lattice_indices[0]).size),
            int(np.unique(registration.model.lattice_indices[1]).size),
        ),
        "lattice_size_resolution_source":(
            "image"
            if hints.count_x is None or hints.count_y is None
            else hints.count_source
        ),
        "lattice_size_inference":registration.diagnostics.get(
            "lattice_size_inference"
        ),
        "registration_search_path":registration.diagnostics.get("search_path"),
        "registration_fallback_used":bool(
            registration.diagnostics.get("fallback_used",False)
        ),
        "detected_spot_count":int(registration.detections.positions_px.shape[1]),
        "matched_count":int(np.count_nonzero(registration.matched_mask)),
        "missing_count":int(np.count_nonzero(~registration.matched_mask)),
        "unmatched_detection_count":int(
            registration.diagnostics.get("unmatched_detection_count",0)
        ),
        "rms_residual_px":float(registration.rms_residual_px),
        "matched_mask":tuple(bool(value) for value in registration.matched_mask),
        "detection_indices":tuple(
            int(value) for value in registration.detection_indices
        ),
        "affine_linear":_matrix_tuple(linear),
        "affine_translation":tuple(float(value) for value in translation),
        "residuals_px":_matrix_tuple(registration.residuals_px),
        "detected_positions_px":_matrix_tuple(
            registration.detections.positions_px
        ),
        "reused_exact":False,
    }

    return LocalizationResult(
        target_type=target_type,
        target_params=target_params,
        parameters=parameters,
        lattice_indices=registration.model.lattice_indices,
        crop_coord=registration.detections.crop_coord,
        cropped_image=registration.detections.cropped_image,
        expected_positions_px=registration.expected_positions_px,
        measured_positions_px=registration.measured_positions_px,
        period_x_px=float(periods[0]),
        period_y_px=float(periods[1]),
        offset_x_px=float(translation[0]),
        offset_y_px=float(translation[1]),
        reused_previous=False,
        diagnostics=diagnostics,
    )

def reuse_localization(
    measurement: ImageMeasurement,
    previous: LocalizationResult,
) -> LocalizationResult:
    """Reuse accepted geometry exactly on a new image measurement."""
    if not isinstance(measurement,ImageMeasurement):
        raise TypeError("measurement must be an ImageMeasurement")
    if previous is None:
        raise RuntimeError("No previous localization is available to reuse")

    image = np.asarray(measurement.image,dtype=np.float64)
    y1,y2,x1,x2 = (int(value) for value in previous.crop_coord)
    if not (0 <= y1 < y2 <= image.shape[0] and 0 <= x1 < x2 <= image.shape[1]):
        raise ValueError(
            "Previous localization crop is outside the current measurement; "
            "run a new localization instead"
        )

    cropped = np.array(image[y1:y2,x1:x2],dtype=np.float64,copy=True)
    diagnostics = dict(previous.diagnostics or {})
    diagnostics.update({
        "acquisition_source":measurement.source,
        "acquisition_created_at":measurement.created_at,
        "measurement_source":measurement.source,
        "measurement_created_at":measurement.created_at,
        "measurement_id":measurement.measurement_id,
        "measurement_detector":measurement.detector,
        "reused_exact":True,
    })

    return LocalizationResult(
        target_type=previous.target_type,
        target_params=previous.target_params,
        parameters=previous.parameters,
        lattice_indices=previous.lattice_indices,
        crop_coord=previous.crop_coord,
        cropped_image=cropped,
        expected_positions_px=previous.expected_positions_px,
        measured_positions_px=previous.measured_positions_px,
        period_x_px=previous.period_x_px,
        period_y_px=previous.period_y_px,
        offset_x_px=previous.offset_x_px,
        offset_y_px=previous.offset_y_px,
        reused_previous=True,
        diagnostics=diagnostics,
    )

def _matrix_tuple(array: np.ndarray):
    value = np.asarray(array,dtype=np.float64)
    return tuple(tuple(float(item) for item in row) for row in value)


__all__ = [
    "localize_measurement",
    "reuse_localization",
]

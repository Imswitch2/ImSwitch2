"""Deterministic post-registration ordering for finite localization lattices.

Registration solves detector geometry and target correspondence.  A finite lattice
can still admit several exactly equivalent index conventions (for example left-to-
right versus right-to-left).  This module normalizes those *representations* after
registration without rerunning detection or changing the fitted physical point set.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import numpy as np

from .model import LatticeRegistration,LocalizationResult


class SpotOrdering(str,Enum):
    """Topology-preserving ways to number one already fitted rectangular lattice."""

    CANONICAL = "canonical"
    FLIP_X = "flip_x"
    FLIP_Y = "flip_y"
    ROTATE_180 = "rotate_180"
    TRANSPOSE = "transpose"
    ROTATE_90_CW = "rotate_90_cw"
    ROTATE_90_CCW = "rotate_90_ccw"
    ANTI_TRANSPOSE = "anti_transpose"

    @classmethod
    def normalize(cls,value: Any) -> "SpotOrdering":
        if isinstance(value,cls):
            return value
        text = str(value or cls.CANONICAL.value).strip().lower()
        aliases = {
            "identity":cls.CANONICAL.value,
            "none":cls.CANONICAL.value,
            "flip_horizontal":cls.FLIP_X.value,
            "flip_vertical":cls.FLIP_Y.value,
            "180":cls.ROTATE_180.value,
            "90_cw":cls.ROTATE_90_CW.value,
            "90_ccw":cls.ROTATE_90_CCW.value,
        }
        return cls(aliases.get(text,text))


_ORDERING_LABELS = {
    SpotOrdering.CANONICAL:"Canonical",
    SpotOrdering.FLIP_X:"Flip X",
    SpotOrdering.FLIP_Y:"Flip Y",
    SpotOrdering.ROTATE_180:"Rotate 180°",
    SpotOrdering.TRANSPOSE:"Swap X/Y",
    SpotOrdering.ROTATE_90_CW:"Rotate 90° CW",
    SpotOrdering.ROTATE_90_CCW:"Rotate 90° CCW",
    SpotOrdering.ANTI_TRANSPOSE:"Swap X/Y + 180°",
}


def spot_ordering_label(value: SpotOrdering | str) -> str:
    return _ORDERING_LABELS[SpotOrdering.normalize(value)]


def spot_ordering_permutation(
    lattice_indices: np.ndarray,
    ordering: SpotOrdering | str,
) -> np.ndarray:
    """Return source-column indices for one lattice ordering.

    The returned permutation has the same convention as feedback orientation:
    ``new_values = old_values[:, permutation]``.
    """
    ordering = SpotOrdering.normalize(ordering)
    indices = np.asarray(lattice_indices)
    if indices.ndim != 2 or indices.shape[0] != 2:
        raise ValueError("lattice_indices must have shape (2, N)")
    count = int(indices.shape[1])
    if count == 0 or ordering is SpotOrdering.CANONICAL:
        return np.arange(count,dtype=np.int64)

    grid = _index_grid(indices)
    if ordering is SpotOrdering.FLIP_X:
        transformed = np.fliplr(grid)
    elif ordering is SpotOrdering.FLIP_Y:
        transformed = np.flipud(grid)
    elif ordering is SpotOrdering.ROTATE_180:
        transformed = np.rot90(grid,2)
    elif ordering is SpotOrdering.TRANSPOSE:
        transformed = grid.T
    elif ordering is SpotOrdering.ROTATE_90_CW:
        transformed = np.rot90(grid,-1)
    elif ordering is SpotOrdering.ROTATE_90_CCW:
        transformed = np.rot90(grid,1)
    elif ordering is SpotOrdering.ANTI_TRANSPOSE:
        transformed = np.fliplr(np.flipud(grid)).T
    else:  # pragma: no cover
        raise ValueError("Unsupported spot ordering: %s" % ordering)
    return np.asarray(transformed,dtype=np.int64).ravel()


def available_spot_orderings(
    lattice_indices: np.ndarray,
    logical_positions: np.ndarray | None=None,
) -> tuple[SpotOrdering,...]:
    """Return ordering symmetries that preserve this finite lattice topology.

    If the registration model's logical coordinates are available, candidate
    symmetries must map those coordinates onto themselves by an affine transform.
    This automatically rejects topology-changing axis swaps (for example a 5x4
    grid represented as 4x5) and motif/stagger transformations that are not true
    symmetries of the fitted finite target.
    """
    indices = np.asarray(lattice_indices)
    _index_grid(indices)  # validate completeness once
    candidates = tuple(SpotOrdering)
    if logical_positions is None:
        x_count = int(np.unique(indices[0]).size)
        y_count = int(np.unique(indices[1]).size)
        return tuple(
            candidate for candidate in candidates
            if candidate in (
                SpotOrdering.CANONICAL,SpotOrdering.FLIP_X,
                SpotOrdering.FLIP_Y,SpotOrdering.ROTATE_180,
            ) or x_count == y_count
        )

    logical = np.asarray(logical_positions,dtype=np.float64)
    if logical.shape != indices.shape:
        raise ValueError("logical_positions must align with lattice_indices")
    span = float(np.max(np.ptp(logical,axis=1))) if logical.shape[1] else 1.0
    tolerance = 1e-7*max(1.0,span)
    valid = []
    for candidate in candidates:
        permutation = spot_ordering_permutation(indices,candidate)
        _linear,_translation,rms = _fit_affine(logical,logical[:,permutation])
        if rms <= tolerance:
            valid.append(candidate)
    return tuple(valid)


def _detector_frame_for_registration(
    logical_positions: np.ndarray,
    detector_positions: np.ndarray,
) -> SpotOrdering:
    """Identify the residual D4 logical frame once at localization time.

    This does not reorder detector points.  It records which abstract lattice
    frame the accepted localization uses relative to the canonical camera basis.
    Downstream feedback must consume this explicit result rather than re-infer
    orientation from detector geometry.
    """
    logical=np.asarray(logical_positions,dtype=np.float64)
    detector=np.asarray(detector_positions,dtype=np.float64)
    best=None
    for priority,candidate in enumerate(SpotOrdering):
        permutation_matrix={
            SpotOrdering.CANONICAL:np.array([[1.0,0.0],[0.0,1.0]]),
            SpotOrdering.FLIP_X:np.array([[-1.0,0.0],[0.0,1.0]]),
            SpotOrdering.FLIP_Y:np.array([[1.0,0.0],[0.0,-1.0]]),
            SpotOrdering.ROTATE_180:np.array([[-1.0,0.0],[0.0,-1.0]]),
            SpotOrdering.TRANSPOSE:np.array([[0.0,1.0],[1.0,0.0]]),
            SpotOrdering.ROTATE_90_CW:np.array([[0.0,-1.0],[1.0,0.0]]),
            SpotOrdering.ROTATE_90_CCW:np.array([[0.0,1.0],[-1.0,0.0]]),
            SpotOrdering.ANTI_TRANSPOSE:np.array([[0.0,-1.0],[-1.0,0.0]]),
        }[candidate]
        transformed=permutation_matrix@logical
        linear,_translation,rms=_fit_affine(transformed,detector)
        score=(*_canonical_basis_score(linear),round(float(rms),9),priority)
        if best is None or score<best[0]:
            best=(score,candidate)
    return SpotOrdering.CANONICAL if best is None else best[1]


def canonicalize_registration(registration: LatticeRegistration) -> LatticeRegistration:
    """Normalize one successful registration to a deterministic camera convention.

    The physical fit is not searched again.  We enumerate only exact affine
    symmetries of the already accepted finite model, choose the representation
    whose logical +X axis points closest to detector-right and then prefer the
    canonical positive handedness (+Y detector-down), and permute target-aligned
    arrays accordingly.
    """
    if not isinstance(registration,LatticeRegistration):
        raise TypeError("registration must be a LatticeRegistration")
    logical = np.asarray(registration.model.logical_positions,dtype=np.float64)
    try:
        options = available_spot_orderings(
            registration.model.lattice_indices,logical,
        )
    except ValueError:
        # Ordering normalization is an optional refinement for complete finite
        # rectangular lattices. Never make other localization target families
        # fail merely because they do not expose that topology.
        return registration
    if not options:
        return registration

    best = None
    for candidate in options:
        permutation = spot_ordering_permutation(
            registration.model.lattice_indices,candidate,
        )
        expected = np.asarray(
            registration.expected_positions_px,dtype=np.float64,
        )[:,permutation]
        linear,translation,_rms = _fit_affine(logical,expected)
        score = _canonical_basis_score(linear)
        item = (score,candidate,permutation,linear,translation)
        if best is None or item[0] < best[0]:
            best = item

    _score,raw_transform,permutation,linear,translation = best
    matched = np.asarray(registration.matched_mask,dtype=bool)[permutation]
    expected = np.asarray(registration.expected_positions_px,dtype=np.float64)[:,permutation]
    measured = np.asarray(registration.measured_positions_px,dtype=np.float64)[:,permutation]
    residuals = measured-expected
    residuals[:,~matched] = 0.0
    norms = np.sqrt(np.sum(residuals[:,matched]**2,axis=0)) if np.any(matched) else np.array([])
    rms = float(np.sqrt(np.mean(norms*norms))) if norms.size else float("inf")

    detector_frame=_detector_frame_for_registration(logical,expected)
    diagnostics = dict(registration.diagnostics or {})
    diagnostics.update({
        "spot_ordering_mode":"auto",
        "spot_ordering":SpotOrdering.CANONICAL.value,
        "spot_ordering_registration_transform":raw_transform.value,
        "spot_ordering_detector_frame":detector_frame.value,
        "spot_ordering_available":tuple(item.value for item in options),
        "ordering_logical_positions":_matrix_tuple(logical),
    })
    return LatticeRegistration(
        model=registration.model,
        detections=registration.detections,
        affine_linear=linear,
        affine_translation=translation,
        expected_positions_px=expected,
        measured_positions_px=measured,
        matched_mask=matched,
        detection_indices=np.asarray(
            registration.detection_indices,dtype=np.int64,
        )[permutation],
        residuals_px=residuals,
        rms_residual_px=rms,
        reused_previous=registration.reused_previous,
        diagnostics=diagnostics,
    )


def reorder_localization(
    localization: LocalizationResult,
    ordering: SpotOrdering | str,
    *,
    automatic: bool=False,
) -> LocalizationResult:
    """Reinterpret one localization using a manual topology-preserving ordering.

    This is a pure column permutation of an already accepted physical fit.  It
    never detects spots or reruns registration.  Switching between manual choices
    is non-cumulative: every choice is defined relative to canonical ordering.
    """
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    requested = SpotOrdering.normalize(ordering)
    diagnostics = dict(localization.diagnostics or {})
    logical = _logical_positions_for_localization(localization,diagnostics)
    available = available_spot_orderings(localization.lattice_indices,logical)
    if requested not in available:
        raise ValueError(
            "Spot ordering '%s' is not a symmetry of this finite lattice"
            % requested.value
        )

    current = SpotOrdering.normalize(
        diagnostics.get("spot_ordering",SpotOrdering.CANONICAL.value)
    )
    if current not in available:
        current = SpotOrdering.CANONICAL
    current_perm = spot_ordering_permutation(localization.lattice_indices,current)
    target_perm = spot_ordering_permutation(localization.lattice_indices,requested)
    inverse_current = np.empty_like(current_perm)
    inverse_current[current_perm] = np.arange(current_perm.size,dtype=np.int64)
    relative = inverse_current[target_perm]

    expected = np.asarray(localization.expected_positions_px,dtype=np.float64)[:,relative]
    measured = np.asarray(localization.measured_positions_px,dtype=np.float64)[:,relative]
    linear,translation,_fit_rms = _fit_affine(logical,expected)

    for key in ("matched_mask","inferred_mask","detection_indices"):
        value = diagnostics.get(key)
        if value is None:
            continue
        array = np.asarray(value)
        if array.shape == (relative.size,):
            diagnostics[key] = tuple(array[relative].tolist())
    residual_value = diagnostics.get("residuals_px")
    if residual_value is not None:
        residuals = np.asarray(residual_value,dtype=np.float64)
        if residuals.shape == expected.shape:
            diagnostics["residuals_px"] = _matrix_tuple(residuals[:,relative])

    diagnostics.update({
        "spot_ordering_mode":"auto" if automatic else "manual",
        "spot_ordering":requested.value,
        "spot_ordering_available":tuple(item.value for item in available),
        "ordering_logical_positions":_matrix_tuple(logical),
        "affine_linear":_matrix_tuple(linear),
        "affine_translation":tuple(float(value) for value in translation),
    })
    return LocalizationResult(
        target_type=localization.target_type,
        target_params=localization.target_params,
        parameters=localization.parameters,
        lattice_indices=localization.lattice_indices,
        crop_coord=localization.crop_coord,
        cropped_image=localization.cropped_image,
        expected_positions_px=expected,
        measured_positions_px=measured,
        period_x_px=float(np.linalg.norm(linear[:,0])),
        period_y_px=float(np.linalg.norm(linear[:,1])),
        offset_x_px=float(translation[0]),
        offset_y_px=float(translation[1]),
        reused_previous=localization.reused_previous,
        diagnostics=diagnostics,
    )


def _logical_positions_for_localization(
    localization: LocalizationResult,diagnostics: dict[str,Any],
) -> np.ndarray:
    value = diagnostics.get("ordering_logical_positions")
    if value is not None:
        logical = np.asarray(value,dtype=np.float64)
        if logical.shape == localization.lattice_indices.shape:
            return logical
    # Compatibility fallback for older results.  The current target families use
    # integer lattice coordinates plus optional alternating-row stagger.
    indices = np.asarray(localization.lattice_indices,dtype=np.float64)
    logical = np.array(indices,copy=True)
    stagger = diagnostics.get("resolved_stagger")
    if stagger is not None:
        logical[0] += float(stagger)*(np.asarray(indices[1],dtype=np.int64)%2)
    logical -= np.mean(logical,axis=1,keepdims=True)
    return logical


def _index_grid(indices: np.ndarray) -> np.ndarray:
    indices = np.asarray(indices)
    if indices.ndim != 2 or indices.shape[0] != 2:
        raise ValueError("lattice_indices must have shape (2, N)")
    x_values = np.unique(indices[0])
    y_values = np.unique(indices[1])
    if int(x_values.size*y_values.size) != int(indices.shape[1]):
        raise ValueError("Spot ordering requires a complete rectangular lattice")
    x_rank = {int(value):rank for rank,value in enumerate(x_values)}
    y_rank = {int(value):rank for rank,value in enumerate(y_values)}
    grid = np.full((int(y_values.size),int(x_values.size)),-1,dtype=np.int64)
    for column in range(indices.shape[1]):
        x = x_rank[int(indices[0,column])]
        y = y_rank[int(indices[1,column])]
        if grid[y,x] >= 0:
            raise ValueError("Lattice indices contain duplicate cells")
        grid[y,x] = column
    if np.any(grid < 0):
        raise ValueError("Lattice indices do not form a complete rectangular grid")
    return grid


def _fit_affine(source: np.ndarray,target: np.ndarray):
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[0] != 2:
        raise ValueError("Affine point sets must share shape (2, N)")
    if source.shape[1] < 3:
        raise ValueError("At least three points are required for affine fitting")
    design = np.vstack((source,np.ones((1,source.shape[1]),dtype=np.float64))).T
    coefficients,_,_,_ = np.linalg.lstsq(design,target.T,rcond=None)
    linear = coefficients[:2,:].T
    translation = coefficients[2,:]
    fitted = linear@source + translation[:,None]
    residual = target-fitted
    rms = float(np.sqrt(np.mean(np.sum(residual*residual,axis=0))))
    return linear,translation,rms


def _canonical_basis_score(linear: np.ndarray):
    linear = np.asarray(linear,dtype=np.float64)
    a = linear[:,0]
    b = linear[:,1]
    angle_x = _wrapped_abs_degrees(np.degrees(np.arctan2(a[1],a[0])))
    determinant = float(np.linalg.det(linear))
    handedness_penalty = 0 if determinant >= 0.0 else 1
    angle_y = _wrapped_abs_degrees(
        np.degrees(np.arctan2(b[1],b[0]))-90.0
    )
    # +X-to-camera-right is the primary convention.  Handedness then chooses
    # detector-down +Y when the X convention admits two mirror-equivalent forms.
    # Equivalent D4 representations can differ only at floating-point noise
    # level after affine fitting.  Never let ~1e-14 degree noise choose a
    # reflected convention before the handedness tie-breaker.
    return (
        round(float(angle_x),9),
        handedness_penalty,
        round(float(angle_y),9),
    )


def _wrapped_abs_degrees(value: float) -> float:
    wrapped = (float(value)+180.0)%360.0-180.0
    return abs(wrapped)


def _matrix_tuple(array: np.ndarray):
    value = np.asarray(array,dtype=np.float64)
    return tuple(tuple(float(item) for item in row) for row in value)


__all__ = [
    "SpotOrdering",
    "available_spot_orderings",
    "canonicalize_registration",
    "reorder_localization",
    "spot_ordering_label",
    "spot_ordering_permutation",
]

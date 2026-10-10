"""Discrete camera-to-target lattice orientation used by feedback analysis."""

from __future__ import annotations

from enum import Enum

import numpy as np

from ..lattice_geometry import LatticeRepresentation
from ..lattice_mapping import (
    detector_frame_transform,finite_lattice_mapping_candidates,
    lattice_transform_matrix,
)
from ..localization import LocalizationResult


class FeedbackOrientation(str,Enum):
    """How camera-localized lattice identities map onto logical target identities."""

    IDENTITY = "identity"
    FLIP_HORIZONTAL = "flip_horizontal"
    FLIP_VERTICAL = "flip_vertical"
    ROTATE_180 = "rotate_180"
    ROTATE_90_CW = "rotate_90_cw"
    ROTATE_90_CCW = "rotate_90_ccw"
    TRANSPOSE = "transpose"
    ANTI_TRANSPOSE = "anti_transpose"

    @classmethod
    def normalize(cls,value) -> "FeedbackOrientation":
        if isinstance(value,cls):
            return value
        text = str(value or cls.IDENTITY.value).strip().lower()
        aliases = {
            "none":cls.IDENTITY.value,
            "horizontal_flip":cls.FLIP_HORIZONTAL.value,
            "flip_x":cls.FLIP_HORIZONTAL.value,
            "vertical_flip":cls.FLIP_VERTICAL.value,
            "flip_y":cls.FLIP_VERTICAL.value,
            "180":cls.ROTATE_180.value,
            "90_cw":cls.ROTATE_90_CW.value,
            "90_ccw":cls.ROTATE_90_CCW.value,
        }
        return cls(aliases.get(text,text))


def _localization_lattice_representation(
    localization: LocalizationResult,
) -> LatticeRepresentation | None:
    """Return finite lattice topology paired with localization columns when known."""
    diagnostics=dict(localization.diagnostics or {})
    payload=diagnostics.get("lattice_representation")
    if payload is not None:
        try:
            lattice=LatticeRepresentation.from_mapping(payload)
            if np.array_equal(lattice.lattice_indices,localization.lattice_indices):
                return lattice
        except (KeyError,TypeError,ValueError):
            pass

    stagger=diagnostics.get("resolved_stagger")
    if stagger is None:
        stagger=dict(localization.target_params or {}).get("stagger")
    if stagger is None:
        return None
    try:
        return LatticeRepresentation.alternating_rows(
            localization.lattice_indices,float(stagger),
        )
    except (TypeError,ValueError):
        return None


def _target_lattice_representation(
    localization: LocalizationResult,
) -> LatticeRepresentation | None:
    """Return the authoritative target topology captured by the localization."""
    try:
        from ..targets.lattice import LatticeDefinition
        lattice=LatticeDefinition.from_params(localization.target_params).lattice_representation()
        if np.array_equal(lattice.lattice_indices,localization.lattice_indices):
            return lattice
    except (KeyError,TypeError,ValueError):
        pass
    return _localization_lattice_representation(localization)


def _topology_orientation_permutation(
    localization: LocalizationResult,
    orientation: FeedbackOrientation,
    target_lattice: LatticeRepresentation,
) -> tuple[np.ndarray,str,np.ndarray] | None:
    """Map camera-canonical localization columns into target lattice identities.

    Localization canonicalization may leave a staggered motif in a residual D4
    logical frame because not every rectangular ordering is a finite motif
    self-symmetry.  Detect that residual detector frame, compose it with the
    configured camera->target feedback orientation, and resolve the finite motif
    phase without cyclic edge wrapping.
    """
    source_lattice=_localization_lattice_representation(localization)
    if source_lattice is None:
        return None
    if not isinstance(target_lattice,LatticeRepresentation):
        raise TypeError("target_lattice must be a LatticeRepresentation")
    if source_lattice.lattice_indices.shape[1]!=target_lattice.lattice_indices.shape[1]:
        return None
    if not np.array_equal(target_lattice.lattice_indices,localization.lattice_indices):
        return None

    source_frame=detector_frame_transform(
        source_lattice,localization.expected_positions_px,
        np.ones(localization.lattice_indices.shape[1],dtype=bool),
    )
    combined=(
        lattice_transform_matrix(orientation.value)
        @lattice_transform_matrix(source_frame)
    )
    candidates=finite_lattice_mapping_candidates(
        source_lattice,target_lattice,combined,best_overlap_only=True,
    )
    complete=[]
    count=localization.lattice_indices.shape[1]
    for source_indices,translation in candidates:
        source_indices=np.asarray(source_indices,dtype=np.int64)
        if (
            source_indices.shape==(count,)
            and np.all(source_indices>=0)
            and np.unique(source_indices).size==count
        ):
            complete.append((source_indices,np.asarray(translation,dtype=np.float64)))
    if not complete:
        return None
    complete.sort(key=lambda item:float(np.dot(item[1],item[1])))
    permutation,translation=complete[0]
    return permutation,source_frame,translation



def orient_lattice_representation(
    lattice: LatticeRepresentation,
    permutation: np.ndarray,
    orientation: FeedbackOrientation | str,
) -> LatticeRepresentation:
    """Reindex finite topology in lockstep with oriented detector positions.

    Feedback orientation changes which camera-localized point belongs to each
    stable target lattice identity.  For motif lattices (notably staggered rows),
    permuting only detector arrays leaves the topology attached to the wrong
    columns and creates alternating-row edge errors.
    """
    if not isinstance(lattice,LatticeRepresentation):
        raise TypeError("lattice must be a LatticeRepresentation")
    permutation=np.asarray(permutation,dtype=np.int64)
    if permutation.shape!=(lattice.lattice_indices.shape[1],):
        raise ValueError("Lattice orientation permutation has the wrong size")
    diagnostics=dict(lattice.diagnostics or {})
    diagnostics["feedback_orientation"]=FeedbackOrientation.normalize(orientation).value
    diagnostics["feedback_orientation_applied"]=True
    return LatticeRepresentation(
        lattice_indices=lattice.lattice_indices,
        cell_indices=np.asarray(lattice.cell_indices)[:,permutation],
        basis_indices=np.asarray(lattice.basis_indices)[permutation],
        cell_vectors=lattice.cell_vectors,
        basis_offsets=lattice.basis_offsets,
        diagnostics=diagnostics,
    )


def orient_localization(
    localization: LocalizationResult,
    orientation: FeedbackOrientation | str,
    *,
    target_lattice: LatticeRepresentation | None=None,
) -> LocalizationResult:
    """Return localization arrays in authoritative target-site order.

    Rectangular index permutations are retained as a compatibility fallback.
    When finite lattice topology is available, the mapping is motif-aware: the
    residual detector frame left by localization canonicalization is composed
    with the configured camera->target orientation and matched to the target
    lattice with a non-wrapping finite phase map.  Downstream reference
    resolution and detector->kxy analysis then consume the same target ordering.
    """
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    orientation = FeedbackOrientation.normalize(orientation)
    if target_lattice is None:
        target_lattice=_target_lattice_representation(localization)

    topology_mapping=(
        None if target_lattice is None
        else _topology_orientation_permutation(localization,orientation,target_lattice)
    )
    diagnostics = dict(localization.diagnostics or {})
    if topology_mapping is not None:
        permutation,source_frame,logical_translation=topology_mapping
        diagnostics["lattice_representation"]=target_lattice.to_dict()
        diagnostics["feedback_orientation_topology_aware"]=True
        diagnostics["feedback_source_detector_frame"]=source_frame
        diagnostics["feedback_logical_translation"]=tuple(
            float(value) for value in logical_translation
        )
    else:
        if orientation is FeedbackOrientation.IDENTITY:
            return localization
        permutation = orientation_permutation(
            localization.lattice_indices,orientation,
        )
        lattice=_localization_lattice_representation(localization)
        if lattice is not None:
            diagnostics["lattice_representation"]=orient_lattice_representation(
                lattice,permutation,orientation,
            ).to_dict()
        diagnostics["feedback_orientation_topology_aware"]=False
    for key in ("matched_mask","inferred_mask","detection_indices"):
        value = diagnostics.get(key)
        if value is not None:
            array = np.asarray(value)
            if array.shape == (permutation.size,):
                diagnostics[key] = tuple(array[permutation].tolist())
    residuals = diagnostics.get("residuals_px")
    if residuals is not None:
        array = np.asarray(residuals,dtype=np.float64)
        if array.shape == localization.expected_positions_px.shape:
            diagnostics["residuals_px"] = tuple(
                tuple(float(item) for item in row)
                for row in array[:,permutation]
            )
    diagnostics["feedback_orientation"] = orientation.value

    return LocalizationResult(
        target_type=localization.target_type,
        target_params=localization.target_params,
        parameters=localization.parameters,
        lattice_indices=localization.lattice_indices,
        crop_coord=localization.crop_coord,
        cropped_image=localization.cropped_image,
        expected_positions_px=localization.expected_positions_px[:,permutation],
        measured_positions_px=localization.measured_positions_px[:,permutation],
        period_x_px=localization.period_x_px,
        period_y_px=localization.period_y_px,
        offset_x_px=localization.offset_x_px,
        offset_y_px=localization.offset_y_px,
        reused_previous=localization.reused_previous,
        diagnostics=diagnostics,
    )


def orientation_permutation(
    lattice_indices: np.ndarray,
    orientation: FeedbackOrientation | str,
) -> np.ndarray:
    """Return source-column indices in logical target order."""
    orientation = FeedbackOrientation.normalize(orientation)
    indices = np.asarray(lattice_indices)
    if indices.ndim != 2 or indices.shape[0] != 2:
        raise ValueError("lattice_indices must have shape (2, N)")
    count = int(indices.shape[1])
    if count == 0 or orientation is FeedbackOrientation.IDENTITY:
        return np.arange(count,dtype=np.int64)

    x_values = np.unique(indices[0])
    y_values = np.unique(indices[1])
    nx = int(x_values.size)
    ny = int(y_values.size)
    if nx * ny != count:
        raise ValueError(
            "Feedback orientation currently requires a complete rectangular lattice"
        )

    x_rank = {int(value):rank for rank,value in enumerate(x_values)}
    y_rank = {int(value):rank for rank,value in enumerate(y_values)}
    grid = np.full((ny,nx),-1,dtype=np.int64)
    for column in range(count):
        try:
            x = x_rank[int(indices[0,column])]
            y = y_rank[int(indices[1,column])]
        except Exception as error:
            raise ValueError("Lattice indices must be finite integer identifiers") from error
        if grid[y,x] >= 0:
            raise ValueError("Lattice indices contain duplicate cells")
        grid[y,x] = column
    if np.any(grid < 0):
        raise ValueError("Lattice indices do not form a complete rectangular grid")

    if orientation is FeedbackOrientation.FLIP_HORIZONTAL:
        transformed = np.fliplr(grid)
    elif orientation is FeedbackOrientation.FLIP_VERTICAL:
        transformed = np.flipud(grid)
    elif orientation is FeedbackOrientation.ROTATE_180:
        transformed = np.rot90(grid,2)
    elif orientation is FeedbackOrientation.ROTATE_90_CW:
        transformed = np.rot90(grid,-1)
    elif orientation is FeedbackOrientation.ROTATE_90_CCW:
        transformed = np.rot90(grid,1)
    elif orientation is FeedbackOrientation.TRANSPOSE:
        transformed = grid.T
    elif orientation is FeedbackOrientation.ANTI_TRANSPOSE:
        transformed = np.fliplr(np.flipud(grid)).T
    else:  # pragma: no cover - Enum normalization makes this unreachable.
        raise ValueError("Unsupported feedback orientation: %s" % orientation)
    return np.asarray(transformed,dtype=np.int64).ravel()


def orientation_reindex_permutation(
    lattice_indices: np.ndarray,
    source_orientation: FeedbackOrientation | str,
    target_orientation: FeedbackOrientation | str,
) -> np.ndarray:
    """Reindex source-oriented columns into target-oriented logical order.

    ``orient_localization`` stores detector positions in the logical order for
    one section's feedback orientation.  A saved reference may later be used by
    a section with a different feedback orientation.  This permutation maps
    values that are already in ``source_orientation`` order into the order
    expected by ``target_orientation`` without changing detector coordinates.
    """
    source = orientation_permutation(lattice_indices,source_orientation)
    target = orientation_permutation(lattice_indices,target_orientation)
    if source.size != target.size:  # pragma: no cover - same lattice by construction.
        raise ValueError("Feedback orientation permutations have different sizes")
    inverse_source = np.empty_like(source)
    inverse_source[source] = np.arange(source.size,dtype=np.int64)
    return inverse_source[target]


__all__ = [
    "FeedbackOrientation",
    "orient_localization",
    "orientation_permutation",
    "orientation_reindex_permutation",
    "orient_lattice_representation",
]

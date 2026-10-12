"""Discrete camera-to-target lattice orientation used by feedback analysis."""

from __future__ import annotations

from enum import Enum

import numpy as np

from ..lattice_geometry import LatticeRepresentation
from ..lattice_mapping import lattice_transform_matrix
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

    Localization records any residual D4 detector frame once.  Feedback composes
    that explicit frame with the configured camera->target orientation and applies
    one rectangular identity permutation.  No motif/phase matching occurs here;
    motif correspondence belongs to reference resolution after both sides are
    target-indexed.
    """
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    orientation=FeedbackOrientation.normalize(orientation)
    if target_lattice is None:
        target_lattice=_target_lattice_representation(localization)

    diagnostics=dict(localization.diagnostics or {})
    source_frame=str(diagnostics.get("spot_ordering_detector_frame","canonical"))
    combined=(
        lattice_transform_matrix(orientation.value)
        @ lattice_transform_matrix(source_frame)
    )
    matrix_to_orientation={
        tuple(lattice_transform_matrix(value.value).ravel()):value
        for value in FeedbackOrientation
    }
    effective=matrix_to_orientation.get(tuple(combined.ravel()))
    if effective is None:
        raise RuntimeError("Unsupported composed feedback orientation")
    permutation=orientation_permutation(localization.lattice_indices,effective)

    if effective is FeedbackOrientation.IDENTITY and target_lattice is None:
        return localization

    if target_lattice is not None:
        diagnostics["lattice_representation"]=target_lattice.to_dict()
    diagnostics["feedback_source_detector_frame"]=source_frame
    diagnostics["feedback_effective_orientation"]=effective.value
    diagnostics["feedback_orientation_topology_aware"]=False
    for key in ("matched_mask","inferred_mask","detection_indices"):
        value=diagnostics.get(key)
        if value is not None:
            array=np.asarray(value)
            if array.shape==(permutation.size,):
                diagnostics[key]=tuple(array[permutation].tolist())
    residuals=diagnostics.get("residuals_px")
    if residuals is not None:
        array=np.asarray(residuals,dtype=np.float64)
        if array.shape==localization.expected_positions_px.shape:
            diagnostics["residuals_px"]=tuple(
                tuple(float(item) for item in row)
                for row in array[:,permutation]
            )
    diagnostics["feedback_orientation"]=orientation.value
    diagnostics["feedback_target_indexed"]=True

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

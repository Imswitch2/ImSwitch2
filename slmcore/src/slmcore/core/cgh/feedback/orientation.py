"""Discrete camera-to-target lattice orientation used by feedback analysis."""

from __future__ import annotations

from enum import Enum

import numpy as np

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


def orient_localization(
    localization: LocalizationResult,
    orientation: FeedbackOrientation | str,
) -> LocalizationResult:
    """Return target-indexed localization for one discrete camera orientation.

    The acquired/cropped image itself is deliberately left untouched.  Only the
    correspondence between logical target indices and localized camera points is
    permuted.  This is equivalent to rotating/flipping the camera image before
    localization, while preserving native camera coordinates for display.
    """
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    orientation = FeedbackOrientation.normalize(orientation)
    if orientation is FeedbackOrientation.IDENTITY:
        return localization

    permutation = orientation_permutation(
        localization.lattice_indices,orientation,
    )
    diagnostics = dict(localization.diagnostics or {})
    for key in ("matched_mask","detection_indices"):
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


__all__ = [
    "FeedbackOrientation",
    "orient_localization",
    "orientation_permutation",
]

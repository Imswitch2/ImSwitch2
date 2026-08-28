"""Finite lattice construction and image-derived lattice geometry.

This module describes the lattice *being registered*: canonical finite lattice
indices, the logical :class:`LatticeModel`, and lightweight inference of finite
lattice shape/orientation from unordered detections.  Numerical affine search,
matching and refinement live in :mod:`slmcore.core.cgh.localization.registration`.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from ..lattice_geometry import LatticeRepresentation
from .model import (
    DetectedSpots,
    LatticeModel,
)

def rectangular_lattice_indices(count_x,count_y):
    """Return canonical row-major integer indices for one finite lattice."""
    nx = int(count_x)
    ny = int(count_y)
    if nx <= 0 or ny <= 0:
        raise ValueError("count_x/count_y must be > 0")
    i = np.arange(nx,dtype=np.int64)
    j = np.arange(ny,dtype=np.int64)
    grid_i,grid_j = np.meshgrid(i,j,indexing="xy")
    return np.vstack([grid_i.ravel(),grid_j.ravel()])

def infer_lattice_shape(detections):
    """Infer finite ``(count_x, count_y)`` from unordered lattice detections.

    Dominant local-neighbour directions provide candidate row directions. For
    each one, points are projected onto the row normal and split at gaps that
    are large relative to the nearest-neighbour period. The most uniform row
    decomposition is retained, with a weak image-horizontal convention used
    only to resolve the otherwise arbitrary X/Y axis swap.
    """
    points = np.asarray(detections.positions_px,dtype=np.float64)
    if points.ndim != 2 or points.shape[0] != 2 or points.shape[1] < 4:
        raise RuntimeError("At least four detected spots are required to infer lattice size")

    tree = cKDTree(points.T)
    distances,_nearest = tree.query(points.T,k=2)
    nearest_scale = float(np.median(distances[:,1]))
    if not np.isfinite(nearest_scale) or nearest_scale <= 0:
        raise RuntimeError("Could not determine a finite nearest-neighbour lattice scale")

    best = None
    for direction in _dominant_neighbor_directions(points):
        candidate = _infer_shape_for_direction(points,direction,nearest_scale)
        if candidate is None:
            continue
        score,nx,ny,details = candidate
        # Canonicalize X toward image-horizontal. This does not assert target
        # rotation; it only resolves the equivalent axis-swap convention when
        # geometry is completely image-derived.
        angle = abs(_wrap_angle(np.degrees(np.arctan2(direction[1],direction[0]))))
        angle = min(angle,abs(180.0-angle))
        score += 0.01 * min(angle,90.0) / 90.0
        item = (score,nx,ny,direction,details)
        if best is None or item[0] < best[0]:
            best = item

    if best is None:
        raise RuntimeError("Could not infer finite lattice X/Y point counts")

    score,nx,ny,direction,details = best
    diagnostics = dict(details)
    diagnostics.update({
        "source":"image",
        "count_x":int(nx),
        "count_y":int(ny),
        "score":float(score),
        "row_direction":tuple(float(value) for value in direction),
    })
    return (int(nx),int(ny)),diagnostics

def _infer_shape_for_direction(points,direction,nearest_scale):
    direction = np.asarray(direction,dtype=np.float64)
    normal = np.array([-direction[1],direction[0]],dtype=np.float64)
    projection = normal.dot(points)
    order = np.argsort(projection)
    sorted_projection = projection[order]
    gaps = np.diff(sorted_projection)
    if gaps.size == 0:
        return None

    # Within one physical row the normal-coordinate spread is small compared
    # with the nearest-neighbour lattice spacing; inter-row gaps are large.
    split_threshold = 0.25 * float(nearest_scale)
    split_after = np.flatnonzero(gaps > split_threshold)
    boundaries = np.concatenate(([-1],split_after,[sorted_projection.size-1]))
    row_sizes = np.diff(boundaries).astype(np.int64)
    if row_sizes.size < 2 or np.any(row_sizes <= 0):
        return None

    nx = int(np.rint(np.percentile(row_sizes,75.0)))
    ny = int(row_sizes.size)
    if nx < 2 or ny < 2:
        return None

    n = int(points.shape[1])
    expected = int(nx*ny)
    # Permit missing spots, but reject decompositions whose rectangular extent
    # is grossly incompatible with the observed finite point count.
    if expected < int(np.floor(0.85*n)) or expected > int(np.ceil(1.25*n)):
        return None

    count_error = float(np.mean(np.abs(row_sizes-nx)) / max(nx,1))
    product_error = float(abs(expected-n) / max(n,1))
    spread_error = float(np.std(row_sizes) / max(float(np.mean(row_sizes)),1.0))
    score = count_error + product_error + 0.5*spread_error
    return score,nx,ny,{
        "nearest_scale_px":float(nearest_scale),
        "split_threshold_px":float(split_threshold),
        "row_count":ny,
        "median_row_size":float(np.median(row_sizes)),
        "row_size_min":int(np.min(row_sizes)),
        "row_size_max":int(np.max(row_sizes)),
        "count_error":count_error,
        "product_error":product_error,
    }

def make_lattice_model(
    lattice_indices: np.ndarray,
    *,
    stagger: float | None=None,
    basis_offsets: np.ndarray | None=None,
) -> LatticeModel:
    """Create the canonical finite lattice model used by registration.

    Known alternating-row stagger is represented as a two-row translation cell
    plus a motif.  ``stagger=None`` deliberately remains unresolved; the
    registration stage infers it from the detected finite row structure before
    target-specific matching.
    """
    indices = np.asarray(lattice_indices)
    if indices.ndim != 2 or indices.shape[0] != 2 or indices.shape[1] == 0:
        raise ValueError("lattice_indices must have shape (2, N)")

    stagger_known = stagger is not None
    stagger_value = 0.0 if stagger is None else float(stagger)
    representation = LatticeRepresentation.alternating_rows(
        indices,
        stagger_value,
    )
    logical = representation.centered_logical_positions

    if basis_offsets is not None:
        offsets = np.asarray(basis_offsets,dtype=np.float64)
        if offsets.shape != logical.shape:
            raise ValueError("basis_offsets must match lattice_indices shape")
        logical = logical + offsets
        logical = logical - np.mean(logical,axis=1,keepdims=True)

    return LatticeModel(
        lattice_indices=indices,
        logical_positions=logical,
        representation=representation,
        diagnostics={
            "lattice_family":"alternating_rows",
            "stagger":stagger_value,
            "canonical_stagger":representation.diagnostics.get(
                "canonical_stagger",stagger_value
            ),
            "stagger_known":bool(stagger_known),
            "stagger_source":"provided" if stagger_known else "unresolved",
        },
    )

def _dominant_neighbor_directions(points,max_directions=8):
    """Return dominant unoriented local-neighbour directions, canonicalized."""
    points = np.asarray(points,dtype=np.float64)
    count = points.shape[1]
    if count < 4:
        return []
    tree = cKDTree(points.T)
    k = min(9,count)
    distances,nearest = tree.query(points.T,k=k)
    if k <= 1:
        return []
    nearest_scale = float(np.median(distances[:,1]))
    if not np.isfinite(nearest_scale) or nearest_scale <= 0:
        return []

    angles = []
    weights = []
    for rank in range(1,k):
        valid = (
            np.isfinite(distances[:,rank])
            & (distances[:,rank] <= 1.8*nearest_scale)
        )
        source = np.flatnonzero(valid)
        if source.size == 0:
            continue
        vectors = points[:,nearest[source,rank]] - points[:,source]
        angle = np.mod(np.arctan2(vectors[1],vectors[0]),np.pi)
        angles.extend(angle.tolist())
        weights.extend(
            (1.0 / np.maximum(distances[source,rank],1e-9)).tolist()
        )
    if not angles:
        return []

    bins = 180
    histogram,edges = np.histogram(
        np.asarray(angles),bins=bins,range=(0.0,np.pi),
        weights=np.asarray(weights),
    )
    histogram = gaussian_filter(histogram.astype(np.float64),1.5,mode="wrap")
    order = np.argsort(histogram)[::-1]
    result = []
    for index in order:
        theta = 0.5 * (edges[index] + edges[index+1])
        direction = np.array([np.cos(theta),np.sin(theta)],dtype=np.float64)
        # Same canonical sign convention used by the generic FFT orientation:
        # prefer image-right, and image-down only for a vertical direction.
        if direction[0] < 0 or (
            abs(float(direction[0])) < 1e-12 and direction[1] < 0
        ):
            direction = -direction
        if any(abs(float(np.dot(direction,existing))) > 0.995 for existing in result):
            continue
        result.append(direction)
        if len(result) >= int(max_directions):
            break
    return result

def _wrap_angle(angle):
    return (float(angle) + 180.0) % 360.0 - 180.0


__all__ = [
    "infer_lattice_shape",
    "make_lattice_model",
    "rectangular_lattice_indices",
]

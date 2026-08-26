"""Infer finite alternating-row lattice topology directly from detections.

Unknown stagger is a topology problem rather than merely an affine-basis
problem.  These routines use finite row/column structure to recover the
alternating-row motif before the normal registration pipeline continues.
"""

from __future__ import annotations

import numpy as np

from ..lattice import _dominant_neighbor_directions,make_lattice_model
from ..model import LatticeModel
from .refinement import _fit_affine

def _infer_unknown_stagger_model(detections,model):
    """Infer alternating-row stagger and target indexing from detected spots.

    The finite target shape is known from ``lattice_indices``.  Candidate row
    directions come from dominant local neighbour vectors.  For each direction
    detections are grouped into the expected number of rows; complete rows then
    provide stable column ordering.  A linear parity term measures the odd-row
    shift, after which the canonical cell+motif model is refit affinely.

    This is deliberately a finite-target inference step, not a Bravais-basis
    choice.  It therefore distinguishes e.g. a finite 55x55 staggered target
    from an equivalent infinite-lattice primitive basis.
    """
    indices = np.asarray(model.lattice_indices)
    grid = _rectangular_lattice_axes(indices)
    if grid is None:
        return None
    i_values,j_values,index_lookup = grid
    nx = int(i_values.size)
    ny = int(j_values.size)
    points = np.asarray(detections.positions_px,dtype=np.float64)
    if nx < 2 or ny < 2 or points.shape[1] < max(4,nx):
        return None

    directions = _dominant_neighbor_directions(points)
    if not directions:
        return None

    best_rows = None
    for direction in directions:
        row_fit = _fit_rows_for_direction(points,direction,nx,ny)
        if row_fit is None:
            continue
        if best_rows is None or row_fit[0] < best_rows[0]:
            best_rows = row_fit
    if best_rows is None:
        return None

    row_score,direction,row_labels,row_counts,row_rms = best_rows
    complete_labels = [
        label for label in range(ny)
        if int(row_counts[label]) == nx
    ]
    minimum_complete = max(4,int(np.ceil(0.35*ny)))
    if len(complete_labels) < minimum_complete:
        return None

    best = None
    for reverse_rows in (False,True):
        target_row_for_label = {
            label: int(j_values[ny-1-label] if reverse_rows else j_values[label])
            for label in range(ny)
        }

        target_columns = []
        detection_columns = []
        feature_i = []
        feature_j = []
        feature_parity = []

        for label in complete_labels:
            detection_ids = np.flatnonzero(row_labels == label)
            ordered = detection_ids[
                np.argsort(direction.dot(points[:,detection_ids]))
            ]
            if ordered.size != nx:
                continue
            target_j = target_row_for_label[label]
            for rank,detection_index in enumerate(ordered):
                target_i = int(i_values[rank])
                target_index = index_lookup.get((target_i,target_j))
                if target_index is None:
                    continue
                target_columns.append(int(target_index))
                detection_columns.append(int(detection_index))
                feature_i.append(float(target_i))
                feature_j.append(float(target_j))
                feature_parity.append(float(target_j % 2))

        if len(target_columns) < max(6,2*nx):
            continue
        parities = np.asarray(feature_parity,dtype=np.float64)
        if not np.any(parities == 0.0) or not np.any(parities == 1.0):
            continue

        design = np.column_stack([
            np.asarray(feature_i,dtype=np.float64),
            np.asarray(feature_j,dtype=np.float64),
            parities,
            np.ones(len(feature_i),dtype=np.float64),
        ])
        observed = points[:,np.asarray(detection_columns,dtype=np.int64)].T
        coefficients,_,_,_ = np.linalg.lstsq(design,observed,rcond=None)
        axis_x = coefficients[0]
        parity_vector = coefficients[2]
        axis_norm_sq = float(np.dot(axis_x,axis_x))
        if axis_norm_sq <= 1e-12:
            continue

        raw_stagger = float(np.dot(parity_vector,axis_x) / axis_norm_sq)
        perpendicular = parity_vector - raw_stagger * axis_x
        perpendicular_ratio = float(
            np.linalg.norm(perpendicular) / max(np.linalg.norm(axis_x),1e-12)
        )

        # Numerical noise can place an unstaggered lattice infinitesimally below
        # zero.  Test both the clipped finite-target value and the modulo-one
        # value; the constrained finite affine fit selects the correct boundary
        # convention rather than treating 0 and 1 as interchangeable.
        stagger_candidates = []
        clipped = min(1.0,max(0.0,raw_stagger))
        modulo = raw_stagger % 1.0
        for value in (clipped,modulo):
            if not any(abs(value-existing) < 1e-6 for existing in stagger_candidates):
                stagger_candidates.append(value)

        target_columns_array = np.asarray(target_columns,dtype=np.int64)
        detection_columns_array = np.asarray(detection_columns,dtype=np.int64)
        observed_columns = points[:,detection_columns_array]

        for stagger_value in stagger_candidates:
            candidate_model = make_lattice_model(
                indices,stagger=float(stagger_value),
            )
            candidate_logical = np.asarray(
                candidate_model.logical_positions,dtype=np.float64,
            )[:,target_columns_array]
            try:
                linear,translation = _fit_affine(
                    candidate_logical,observed_columns,
                )
            except RuntimeError:
                continue
            residual = observed_columns - (
                linear.dot(candidate_logical) + translation[:,None]
            )
            fit_rms = float(np.sqrt(np.mean(np.sum(residual*residual,axis=0))))

            score = (
                fit_rms
                + 3.0 * perpendicular_ratio * max(np.linalg.norm(axis_x),1.0)
                + 0.25 * float(row_score)
            )
            item = (
                score,candidate_model,linear,translation,
                {
                    "source":"image",
                    "stagger":float(stagger_value),
                    "raw_stagger":float(raw_stagger),
                    "fit_rms_px":fit_rms,
                    "parity_perpendicular_ratio":perpendicular_ratio,
                    "complete_rows":int(len(complete_labels)),
                    "row_count":ny,
                    "row_cluster_rms_fraction":float(row_rms),
                    "row_cluster_score":float(row_score),
                    "row_direction":tuple(float(v) for v in direction),
                    "row_order_reversed":bool(reverse_rows),
                },
            )
            if best is None or item[0] < best[0]:
                best = item

    if best is None:
        return None

    _,resolved_model,linear,translation,diagnostics = best
    merged = dict(resolved_model.diagnostics)
    merged.update({
        "stagger_known":True,
        "stagger_source":"image",
        "stagger_inference":diagnostics,
    })
    resolved_model = LatticeModel(
        lattice_indices=resolved_model.lattice_indices,
        logical_positions=resolved_model.logical_positions,
        representation=resolved_model.representation,
        diagnostics=merged,
    )
    return resolved_model,linear,translation,diagnostics

def _rectangular_lattice_axes(indices):
    """Return sorted target axes and ``(i,j)->column`` lookup for full grids."""
    values = np.rint(np.asarray(indices)).astype(np.int64)
    if not np.allclose(indices,values,rtol=0.0,atol=1e-9):
        return None
    i_values = np.unique(values[0])
    j_values = np.unique(values[1])
    if int(i_values.size * j_values.size) != values.shape[1]:
        return None
    lookup = {}
    for column,(i_value,j_value) in enumerate(values.T):
        key = (int(i_value),int(j_value))
        if key in lookup:
            return None
        lookup[key] = int(column)
    for j_value in j_values:
        for i_value in i_values:
            if (int(i_value),int(j_value)) not in lookup:
                return None
    return i_values,j_values,lookup

def _fit_rows_for_direction(points,direction,nx,ny):
    """Cluster detections into target rows for one candidate row direction."""
    direction = np.asarray(direction,dtype=np.float64)
    normal = np.array([-direction[1],direction[0]],dtype=np.float64)
    projection = normal.dot(points)
    centers,labels = _kmeans_1d(projection,ny)
    counts = np.bincount(labels,minlength=ny)
    if centers.size != ny or np.any(counts == 0):
        return None
    separation = float(np.median(np.diff(centers))) if ny > 1 else 1.0
    if not np.isfinite(separation) or abs(separation) <= 1e-9:
        return None
    residual = projection - centers[labels]
    rms_fraction = float(
        np.sqrt(np.mean(residual*residual)) / abs(separation)
    )
    count_error = float(
        np.mean(np.abs(counts-int(nx))) / max(int(nx),1)
    )
    score = count_error + 2.0*rms_fraction
    return score,direction,labels,counts,rms_fraction

def _kmeans_1d(values,count,max_iterations=20):
    """Small deterministic one-dimensional k-means for ordered lattice rows."""
    values = np.asarray(values,dtype=np.float64)
    if values.size < int(count) or int(count) < 1:
        return np.empty((0,),dtype=np.float64),np.empty(values.shape,dtype=np.int64)
    low,high = np.percentile(values,[0.2,99.8])
    centers = np.linspace(float(low),float(high),int(count))
    labels = np.zeros(values.size,dtype=np.int64)
    for _ in range(int(max_iterations)):
        new_labels = np.argmin(
            np.abs(values[:,None]-centers[None,:]),axis=1,
        ).astype(np.int64)
        new_centers = np.array(centers,copy=True)
        for index in range(int(count)):
            selected = new_labels == index
            if np.any(selected):
                new_centers[index] = float(np.mean(values[selected]))
        order = np.argsort(new_centers)
        inverse = np.empty(int(count),dtype=np.int64)
        inverse[order] = np.arange(int(count),dtype=np.int64)
        new_labels = inverse[new_labels]
        new_centers = new_centers[order]
        if np.array_equal(new_labels,labels) and np.allclose(
            new_centers,centers,rtol=0.0,atol=1e-9,
        ):
            labels,centers = new_labels,new_centers
            break
        labels,centers = new_labels,new_centers
    return centers,labels

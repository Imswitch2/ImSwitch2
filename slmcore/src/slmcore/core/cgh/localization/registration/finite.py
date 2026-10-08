"""Finite rectangular-grid topology inference and registration initialization.

This stage answers a different question from FFT/Bravais-lattice discovery: it
uses the known finite ``Nx x Ny`` target topology to assign detected rows and
columns before affine refinement.  That prevents equivalent infinite-lattice
bases (for example ``b + a``) from silently changing the finite target indexing.
"""

from __future__ import annotations

import numpy as np

from ..lattice import _dominant_neighbor_directions,make_lattice_model
from ..model import LatticeModel
from .refinement import _fit_affine


def _infer_finite_grid_initializer(detections,model):
    """Return a topology-preserving affine initializer for a full finite grid.

    The finite X/Y counts come from ``model.lattice_indices``.  Candidate image
    row directions are inferred from local neighbour vectors, detections are
    clustered into the known number of rows, and complete rows establish an
    unambiguous column ordering.  If stagger is unresolved it is estimated from
    the alternating-row parity term; otherwise the supplied stagger is retained
    exactly.

    Returns ``(resolved_model, linear, translation, diagnostics)`` or ``None``
    when the finite topology cannot be recovered reliably enough to initialize
    registration.
    """
    indices = np.asarray(model.lattice_indices)
    grid = _rectangular_lattice_axes(indices)
    if grid is None:
        return None
    i_values,j_values,index_lookup = grid
    nx = int(i_values.size)
    ny = int(j_values.size)
    points = np.asarray(detections.positions_px,dtype=np.float64)
    if nx < 2 or ny < 2 or points.shape[1] < max(4,min(nx,ny)):
        return None

    directions = _dominant_neighbor_directions(points)
    if not directions:
        return None

    # The target X axis is the direction that produces ``ny`` rows containing
    # approximately ``nx`` points each.  This finite count information resolves
    # the axis/basis ambiguity that an infinite-lattice FFT cannot resolve.
    row_candidates = []
    for direction in directions:
        row_fit = _fit_rows_for_direction(points,direction,nx,ny)
        if row_fit is not None:
            row_candidates.append(row_fit)
    if not row_candidates:
        return None
    row_candidates.sort(key=lambda item:item[0])

    stagger_known = bool(model.diagnostics.get("stagger_known",False))
    supplied_stagger = float(model.diagnostics.get("stagger",0.0) or 0.0)
    best = None

    # Normally the first row decomposition is enough, but trying a few nearly
    # competitive neighbour directions is cheap and makes square grids robust
    # to noisy direction histograms.
    for row_score,direction,row_labels,row_counts,row_rms in row_candidates[:4]:
        complete_labels = [
            label for label in range(ny)
            if int(row_counts[label]) == nx
        ]
        minimum_complete = min(ny,max(2,int(np.ceil(0.25*ny))))
        if len(complete_labels) < minimum_complete:
            continue

        for reverse_rows,reverse_columns in (
            (False,False),(False,True),(True,False),(True,True),
        ):
            correspondences = _finite_grid_correspondences(
                points=points,
                direction=direction,
                row_labels=row_labels,
                complete_labels=complete_labels,
                i_values=i_values,
                j_values=j_values,
                index_lookup=index_lookup,
                reverse_rows=reverse_rows,
                reverse_columns=reverse_columns,
            )
            if correspondences is None:
                continue
            target_columns,detection_columns,features = correspondences
            if target_columns.size < 6:
                continue

            if stagger_known:
                candidate_model = model
                stagger_value = supplied_stagger
                stagger_diagnostics = {
                    "source":str(model.diagnostics.get("stagger_source","provided")),
                    "stagger":float(stagger_value),
                    "raw_stagger":float(stagger_value),
                }
            else:
                inferred = _infer_stagger_from_correspondences(
                    points=points,
                    detection_columns=detection_columns,
                    feature_i=features[0],
                    feature_j=features[1],
                    feature_parity=features[2],
                )
                if inferred is None:
                    continue
                raw_stagger,perpendicular_ratio,axis_norm = inferred
                stagger_candidates = []
                clipped = min(1.0,max(0.0,raw_stagger))
                modulo = raw_stagger % 1.0
                for value in (clipped,modulo):
                    if not any(
                        abs(value-existing) < 1e-6
                        for existing in stagger_candidates
                    ):
                        stagger_candidates.append(value)

            if stagger_known:
                model_candidates = [(candidate_model,stagger_diagnostics,0.0)]
            else:
                model_candidates = []
                for stagger_value in stagger_candidates:
                    candidate_model = make_lattice_model(
                        indices,stagger=float(stagger_value),
                    )
                    model_candidates.append((
                        candidate_model,
                        {
                            "source":"image",
                            "stagger":float(stagger_value),
                            "raw_stagger":float(raw_stagger),
                            "parity_perpendicular_ratio":float(perpendicular_ratio),
                        },
                        3.0 * float(perpendicular_ratio) * max(float(axis_norm),1.0),
                    ))

            observed = points[:,detection_columns]
            for candidate_model,stagger_diagnostics,stagger_penalty in model_candidates:
                candidate_logical = np.asarray(
                    candidate_model.logical_positions,dtype=np.float64,
                )[:,target_columns]
                try:
                    linear,translation = _fit_affine(candidate_logical,observed)
                except RuntimeError:
                    continue
                residual = observed - (
                    linear.dot(candidate_logical) + translation[:,None]
                )
                fit_rms = float(
                    np.sqrt(np.mean(np.sum(residual*residual,axis=0)))
                )
                score = fit_rms + float(stagger_penalty) + 0.25*float(row_score)
                diagnostics = {
                    "source":"finite_grid",
                    "stagger":float(stagger_diagnostics["stagger"]),
                    "raw_stagger":float(stagger_diagnostics["raw_stagger"]),
                    "stagger_source":str(stagger_diagnostics["source"]),
                    "fit_rms_px":fit_rms,
                    "complete_rows":int(len(complete_labels)),
                    "row_count":ny,
                    "row_cluster_rms_fraction":float(row_rms),
                    "row_cluster_score":float(row_score),
                    "row_direction":tuple(float(v) for v in direction),
                    "row_order_reversed":bool(reverse_rows),
                    "column_order_reversed":bool(reverse_columns),
                }
                if "parity_perpendicular_ratio" in stagger_diagnostics:
                    diagnostics["parity_perpendicular_ratio"] = float(
                        stagger_diagnostics["parity_perpendicular_ratio"]
                    )
                item = (score,candidate_model,linear,translation,diagnostics)
                if best is None or item[0] < best[0]:
                    best = item

    if best is None:
        return None

    _,resolved_model,linear,translation,diagnostics = best
    merged = dict(resolved_model.diagnostics)
    if not stagger_known:
        merged.update({
            "stagger_known":True,
            "stagger_source":"image",
            "stagger_inference":diagnostics,
        })
    merged["finite_grid_inference"] = diagnostics
    resolved_model = LatticeModel(
        lattice_indices=resolved_model.lattice_indices,
        logical_positions=resolved_model.logical_positions,
        representation=resolved_model.representation,
        diagnostics=merged,
    )
    return resolved_model,linear,translation,diagnostics


def _finite_grid_correspondences(
    *,points,direction,row_labels,complete_labels,
    i_values,j_values,index_lookup,reverse_rows,reverse_columns,
):
    ny = int(j_values.size)
    nx = int(i_values.size)
    target_row_for_label = {
        label:int(j_values[ny-1-label] if reverse_rows else j_values[label])
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
        if reverse_columns:
            ordered = ordered[::-1]
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

    if not target_columns:
        return None
    return (
        np.asarray(target_columns,dtype=np.int64),
        np.asarray(detection_columns,dtype=np.int64),
        (
            np.asarray(feature_i,dtype=np.float64),
            np.asarray(feature_j,dtype=np.float64),
            np.asarray(feature_parity,dtype=np.float64),
        ),
    )


def _infer_stagger_from_correspondences(
    *,points,detection_columns,feature_i,feature_j,feature_parity,
):
    parities = np.asarray(feature_parity,dtype=np.float64)
    if not np.any(parities == 0.0) or not np.any(parities == 1.0):
        return None
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
        return None
    raw_stagger = float(np.dot(parity_vector,axis_x) / axis_norm_sq)
    perpendicular = parity_vector - raw_stagger*axis_x
    axis_norm = float(np.linalg.norm(axis_x))
    perpendicular_ratio = float(
        np.linalg.norm(perpendicular) / max(axis_norm,1e-12)
    )
    return raw_stagger,perpendicular_ratio,axis_norm


def _rectangular_lattice_axes(indices):
    """Return sorted target axes and ``(i,j)->column`` lookup for full grids."""
    values = np.rint(np.asarray(indices)).astype(np.int64)
    if not np.allclose(indices,values,rtol=0.0,atol=1e-9):
        return None
    i_values = np.unique(values[0])
    j_values = np.unique(values[1])
    if int(i_values.size*j_values.size) != values.shape[1]:
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


__all__ = ["_infer_finite_grid_initializer"]

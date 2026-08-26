"""Refine affine lattice geometry and establish one-to-one spot matches.

Given an initial linear transform and translation, this module iterates local
correspondence and affine fitting, rejects robust outliers, scores candidate
solutions, and constructs the final :class:`LatticeRegistration` result.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from ..lattice import _wrap_angle
from ..model import LatticeRegistration
from .candidates import _period_prior_score

def _excellent_fit(fitted,target_count):
    matched = int(fitted["target_indices"].size)
    allowed_missing = max(1,int(np.ceil(0.002*int(target_count))))
    rms = float(fitted.get("rms_residual_px",float("inf")))
    return matched >= int(target_count)-allowed_missing and rms <= 1.0

def _refine_registration(
    model,points,linear,translation,options,*,tree=None,
):
    """Free affine refinement using local sparse one-to-one correspondences."""
    logical = np.asarray(model.logical_positions,dtype=np.float64)
    points = np.asarray(points,dtype=np.float64)
    linear = np.array(linear,dtype=np.float64,copy=True)
    translation = np.array(translation,dtype=np.float64,copy=True)
    if tree is None:
        tree = cKDTree(points.T)
    last_pairs = None

    for _ in range(int(options.max_iterations)):
        predicted = linear.dot(logical) + translation[:,None]
        periods = np.linalg.norm(linear,axis=0)
        gate = max(
            1.5,
            float(options.matching_gate_fraction) * float(np.min(periods)),
        )
        target_indices,detection_indices,distances = _local_one_to_one_matches(
            predicted,points,gate,tree=tree,k_neighbors=4,
        )
        minimum = max(
            3,int(np.ceil(float(options.min_match_fraction)*logical.shape[1]))
        )
        minimum = min(minimum,logical.shape[1])
        if target_indices.size < minimum:
            return None

        q = logical[:,target_indices]
        p = points[:,detection_indices]
        fitted_linear,fitted_translation = _fit_affine(q,p)
        residual = p - (fitted_linear.dot(q) + fitted_translation[:,None])
        norms = np.sqrt(np.sum(residual*residual,axis=0))
        inliers = _robust_inliers(
            norms,float(options.robust_outlier_sigma),gate,
        )
        if np.count_nonzero(inliers) >= 3 and not np.all(inliers):
            q = q[:,inliers]
            p = p[:,inliers]
            target_indices = target_indices[inliers]
            detection_indices = detection_indices[inliers]
            fitted_linear,fitted_translation = _fit_affine(q,p)

        pairs = (
            tuple(target_indices.tolist()),
            tuple(detection_indices.tolist()),
        )
        converged = (
            last_pairs == pairs
            and np.allclose(
                fitted_linear,linear,rtol=0,atol=1e-5,
            )
            and np.allclose(
                fitted_translation,translation,rtol=0,atol=1e-4,
            )
        )
        linear,translation = fitted_linear,fitted_translation
        last_pairs = pairs
        if converged:
            break

    predicted = linear.dot(logical) + translation[:,None]
    periods = np.linalg.norm(linear,axis=0)
    gate = max(
        1.5,float(options.matching_gate_fraction)*float(np.min(periods)),
    )
    target_indices,detection_indices,distances = _local_one_to_one_matches(
        predicted,points,gate,tree=tree,k_neighbors=4,
    )
    if target_indices.size < 3:
        return None

    residual = points[:,detection_indices] - predicted[:,target_indices]
    norms = np.sqrt(np.sum(residual*residual,axis=0))
    # inliers = _robust_inliers(
    #     norms,float(options.robust_outlier_sigma),gate,
    # )
    # target_indices = target_indices[inliers]
    # detection_indices = detection_indices[inliers]
    # norms = norms[inliers]
    rms = (
        float(np.sqrt(np.mean(norms*norms)))
        if norms.size else float("inf")
    )
    return {
        "linear":linear,
        "translation":translation,
        "target_indices":target_indices,
        "detection_indices":detection_indices,
        "gate":gate,
        "rms_residual_px":rms,
    }

def _local_one_to_one_matches(
    predicted,points,gate,*,tree=None,k_neighbors=4,
):
    """Sparse local one-to-one assignment around each predicted target spot.

    Unlike the original one-nearest greedy matcher, every target contributes a
    few nearby candidates.  If its closest detection has already been claimed,
    the target can therefore use its second/third nearby detection rather than
    being incorrectly labelled missing.
    """
    predicted = np.asarray(predicted,dtype=np.float64)
    points = np.asarray(points,dtype=np.float64)
    if tree is None:
        tree = cKDTree(points.T)

    k = max(1,min(int(k_neighbors),points.shape[1]))
    distances,nearest = tree.query(
        predicted.T,k=k,distance_upper_bound=float(gate),
    )
    if k == 1:
        distances = distances[:,None]
        nearest = nearest[:,None]

    edges = []
    for target_index in range(predicted.shape[1]):
        for rank in range(k):
            distance = float(distances[target_index,rank])
            detection_index = int(nearest[target_index,rank])
            if (
                np.isfinite(distance)
                and detection_index < points.shape[1]
            ):
                edges.append((distance,target_index,detection_index))

    edges.sort(key=lambda item:item[0])
    used_targets = set()
    used_detections = set()
    accepted = []
    for distance,target_index,detection_index in edges:
        if target_index in used_targets or detection_index in used_detections:
            continue
        used_targets.add(target_index)
        used_detections.add(detection_index)
        accepted.append((target_index,detection_index,distance))

    accepted.sort(key=lambda item:item[0])
    return (
        np.asarray([item[0] for item in accepted],dtype=np.int64),
        np.asarray([item[1] for item in accepted],dtype=np.int64),
        np.asarray([item[2] for item in accepted],dtype=np.float64),
    )

def _greedy_matches(predicted,points,gate):
    return _local_one_to_one_matches(
        predicted,points,gate,k_neighbors=4,
    )

def _fit_affine(logical,points):
    design = np.column_stack([logical.T,np.ones(logical.shape[1],dtype=np.float64)])
    coefficients,_,_,_ = np.linalg.lstsq(design,points.T,rcond=None)
    linear = coefficients[:2,:].T
    translation = coefficients[2,:]
    if abs(np.linalg.det(linear)) < 1e-9:
        raise RuntimeError("Localized lattice affine transform is singular")
    return linear,translation

def _robust_inliers(norms,sigma,gate):
    if norms.size < 4:
        return np.ones(norms.shape,dtype=bool)
    median = float(np.median(norms))
    mad = float(np.median(np.abs(norms-median)))
    robust_sigma = 1.4826 * mad
    threshold = min(float(gate),median + float(sigma) * max(robust_sigma,0.25))
    return norms <= threshold

def _fit_score(fitted,options):
    count = fitted["target_indices"].size
    linear = fitted["linear"]
    expected = options.expected_period_px
    prior = _period_prior_score(
        np.linalg.norm(linear,axis=0),expected,options.period_tolerance_fraction,
    )
    q_count = max(1,int(count))
    # Prefer the geometrically cleanest solution when several symmetry-related
    # index assignments explain the same number of detections.
    # The canonical default keeps logical +X pointing approximately image-right;
    # callers may instead supply an expected detector-space rotation.
    angle = float(np.degrees(np.arctan2(linear[1,0],linear[0,0])))
    wanted = 0.0 if options.expected_rotation_deg is None else float(options.expected_rotation_deg)
    orientation_penalty = abs(_wrap_angle(angle-wanted))
    orientation_cost = (float(orientation_penalty) / 45.0) ** 2
    quality = float(prior) - orientation_cost
    rms = float(fitted.get("rms_residual_px",float("inf")))
    return (int(count),quality,-rms)

def _build_result(model,detections,fitted,options,reused_previous):
    logical = np.asarray(model.logical_positions,dtype=np.float64)
    points = np.asarray(detections.positions_px,dtype=np.float64)
    linear = fitted["linear"]
    translation = fitted["translation"]
    expected = linear.dot(logical) + translation[:,None]
    n = logical.shape[1]
    matched = np.zeros(n,dtype=bool)
    detection_indices = np.full(n,-1,dtype=np.int64)
    measured = np.array(expected,copy=True)
    residuals = np.zeros_like(expected)
    target_indices = fitted["target_indices"]
    detection_matches = fitted["detection_indices"]
    matched[target_indices] = True
    detection_indices[target_indices] = detection_matches
    measured[:,target_indices] = points[:,detection_matches]
    residuals[:,target_indices] = measured[:,target_indices] - expected[:,target_indices]
    norms = np.sqrt(np.sum(residuals[:,matched]**2,axis=0)) if np.any(matched) else np.array([])
    rms = float(np.sqrt(np.mean(norms*norms))) if norms.size else float("inf")
    return LatticeRegistration(
        model=model,
        detections=detections,
        affine_linear=linear,
        affine_translation=translation,
        expected_positions_px=expected,
        measured_positions_px=measured,
        matched_mask=matched,
        detection_indices=detection_indices,
        residuals_px=residuals,
        rms_residual_px=rms,
        reused_previous=reused_previous,
        diagnostics={
            "matched_count":int(np.count_nonzero(matched)),
            "missing_count":int(np.count_nonzero(~matched)),
            "unmatched_detection_count":int(len(set(range(points.shape[1])) - set(detection_matches.tolist()))),
            "matching_gate_px":float(fitted["gate"]),
            "expected_period_px":options.expected_period_px,
            "initializer_source":fitted.get("initializer_source"),
            "correlation_score":fitted.get("correlation_score"),
            "search_path":fitted.get("search_path"),
            "fallback_used":bool(fitted.get("fallback_used",False)),
            "resolved_stagger":model.diagnostics.get("stagger"),
            "stagger_source":model.diagnostics.get("stagger_source"),
            "canonical_stagger":model.diagnostics.get("canonical_stagger"),
            "stagger_inference":model.diagnostics.get("stagger_inference"),
            "lattice_count_x":model.diagnostics.get("lattice_count_x"),
            "lattice_count_y":model.diagnostics.get("lattice_count_y"),
            "lattice_size_source":model.diagnostics.get("lattice_size_source"),
            "lattice_size_inference":model.diagnostics.get("lattice_size_inference"),
        },
    )

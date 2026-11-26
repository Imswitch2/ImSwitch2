"""Generate, normalize, and score candidate affine lattice geometries.

This module contains geometry bookkeeping shared by guided and FFT search:
orientation variants, representation symmetries, period-prior scoring, and
deduplication.  It intentionally contains no image processing.
"""

from __future__ import annotations

import numpy as np

from ..lattice import _wrap_angle

def _prior_basis_candidates(options):
    """Build soft geometry initializers when period and orientation are known.

    These are only starting points: the final transform is still freely affine.
    A modest skew sweep prevents an approximately known orthogonal prior from
    failing on genuinely sheared/oblique lattices.
    """
    if options.expected_period_px is None or options.expected_rotation_deg is None:
        return []
    px,py = options.expected_period_px
    theta = np.deg2rad(float(options.expected_rotation_deg))
    rotation = np.array([
        [np.cos(theta),-np.sin(theta)],
        [np.sin(theta), np.cos(theta)],
    ],dtype=np.float64)
    candidates = []
    for skew_deg in (0.0,-10.0,10.0,-20.0,20.0,-30.0,30.0):
        skew = np.deg2rad(skew_deg)
        local = np.column_stack([
            np.array([float(px),0.0]),
            np.array([float(py)*np.sin(skew),float(py)*np.cos(skew)]),
        ])
        candidates.append(rotation.dot(local))
    return candidates

def _representation_period_prior_score(linear,representation,expected,tolerance):
    """Best period-prior score over bases compatible with the lattice motif."""
    if expected is None:
        return 0.0
    best = -np.inf
    for candidate in _representation_affine_variants(linear,representation):
        score = _period_prior_score(
            np.linalg.norm(candidate,axis=0),expected,tolerance,
        )
        best = max(best,float(score))
    return best

def _representation_period_compatible(linear,representation,expected,tolerance):
    """Whether any representation-compatible basis matches the period hint."""
    if expected is None:
        return True
    for candidate in _representation_affine_variants(linear,representation):
        if _period_within_tolerance(
            np.linalg.norm(candidate,axis=0),expected,tolerance,
        ):
            return True
    return False

def _period_within_tolerance(lengths,expected,tolerance):
    """Return whether unordered primitive lengths agree with a period prior."""
    if expected is None:
        return True
    lengths = np.asarray(lengths,dtype=np.float64)
    expected = np.asarray(expected,dtype=np.float64)
    if lengths.shape != (2,) or expected.shape != (2,):
        return False
    tolerance = max(float(tolerance),0.0)
    floor = np.maximum(expected,1e-12)
    for candidate in (lengths,lengths[::-1]):
        relative = np.abs(candidate-expected) / floor
        if np.all(relative <= tolerance):
            return True
    return False

def _period_prior_score(lengths,expected,tolerance):
    if expected is None:
        # With no prior, reciprocal-peak strength should choose the initializer.
        # Preferring a direct-space length here tends to select subharmonics.
        return 0.0
    expected = np.asarray(expected,dtype=np.float64)
    best = -np.inf
    scale = np.maximum(expected * max(float(tolerance),0.05),1.0)
    for candidate in (lengths,lengths[::-1]):
        z = (candidate-expected) / scale
        best = max(best,float(-np.sum(z*z)))
    return best

def _basis_orientations(linear,expected_rotation_deg):
    columns = [linear[:,0],linear[:,1]]
    candidates = []
    for swap in (False,True):
        first,second = (columns[1],columns[0]) if swap else (columns[0],columns[1])
        for sign_first in (-1.0,1.0):
            for sign_second in (-1.0,1.0):
                candidate = np.column_stack([sign_first*first,sign_second*second])
                if np.linalg.det(candidate) <= 0:
                    continue
                angle = float(np.degrees(np.arctan2(candidate[1,0],candidate[0,0])))
                if expected_rotation_deg is None:
                    # Canonical image orientation: logical +X points primarily right.
                    penalty = abs(_wrap_angle(angle))
                else:
                    penalty = abs(_wrap_angle(angle-float(expected_rotation_deg)))
                candidates.append((penalty,candidate))
    candidates.sort(key=lambda item:item[0])
    limit = 1 if expected_rotation_deg is not None else 2
    return [item[1] for item in candidates[:limit]]

def _representation_affine_variants(linear,representation):
    """Return affine initializers compatible with one canonical lattice motif.

    FFT analysis discovers translation symmetries of the observed point set.
    The backend representation may use a larger canonical cell plus a motif, so
    a few algebraically equivalent initializers are generated before the free
    affine refinement.  The localization solver itself no longer treats
    ``stagger`` as a special coordinate-generation rule.
    """
    linear = np.asarray(linear,dtype=np.float64)
    yield linear
    if representation is None:
        return
    diagnostics = getattr(representation,"diagnostics",{})
    if diagnostics.get("family") != "alternating_rows":
        return
    stagger = float(diagnostics.get("canonical_stagger",0.0) or 0.0)
    if abs(stagger) <= 1e-12:
        return
    a = linear[:,0]
    c = linear[:,1]
    variants = (
        np.column_stack([a,c-stagger*a]),
        np.column_stack([a,c+stagger*a]),
        np.column_stack([a,0.5*c]),
    )
    for candidate in variants:
        if abs(float(np.linalg.det(candidate))) > 1e-8:
            yield candidate

def _deduplicate_linear_candidates(candidates):
    unique = {}
    for source,linear in candidates:
        linear = np.asarray(linear,dtype=np.float64)
        if linear.shape != (2,2) or abs(float(np.linalg.det(linear))) < 1e-9:
            continue
        # Geometry from FFT is only pixel-resolution accurate at this stage;
        # coarse quantization removes symmetry-equivalent duplicates.
        key = tuple(np.round(linear.ravel(),decimals=3))
        unique.setdefault(key,(source,linear))
    return list(unique.values())

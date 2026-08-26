"""High-level policy for registering a finite lattice to detected spots.

This module owns the registration strategy and fallback order only. Numerical
subroutines live in the sibling modules: FFT candidate discovery, correlation
initialization, stagger inference, candidate geometry, and affine refinement.
Keeping the policy here makes :func:`register_lattice` read like the algorithm
rather than like its implementation details.
"""

from __future__ import annotations

from dataclasses import replace


import numpy as np
from scipy.spatial import cKDTree

from ..model import (
    DetectedSpots,
    LatticeModel,
    LatticeRegistration,
    LatticeRegistrationOptions,
)
from .candidates import _period_within_tolerance
from .correlation import (
    _CorrelationWorkspace,
    _best_period_neighbor_translation,
    _initial_translation,
)
from .refinement import _build_result,_fit_score,_refine_registration
from .search import _search_fft_registration
from .stagger import _infer_unknown_stagger_model

def register_lattice(
    detections: DetectedSpots,
    model: LatticeModel,
    options: LatticeRegistrationOptions=LatticeRegistrationOptions(),
    *,
    previous: LatticeRegistration | None=None,
    initial_linear: np.ndarray | None=None,
    initial_translation: np.ndarray | None=None,
) -> LatticeRegistration:
    """Register indexed lattice points with guided search + global fallbacks.

    ``expected_period_px`` is treated as strong search guidance, never as a
    fixed final geometry.  When present, a period-filtered FFT search is tried
    first.  If it cannot produce a strong fit, registration automatically falls
    back to the target-independent fast search and finally to the broad/full
    search.  The logical model (including a known stagger) is retained through
    every fallback.
    """
    points = np.asarray(detections.positions_px,dtype=np.float64)
    if points.shape[1] < 3:
        raise RuntimeError(
            "At least three detected spots are required for lattice registration"
        )

    logical = np.asarray(model.logical_positions,dtype=np.float64)
    tree = cKDTree(points.T)
    correlation = _CorrelationWorkspace(detections.processed_image)


    # Unknown stagger is not equivalent to zero stagger.  Resolve the finite
    # alternating-row representation directly from detected row structure
    # before target-specific registration.  The inferred affine transform is
    # also an excellent initializer, so the common case avoids FFT/template
    # search entirely.
    if not bool(model.diagnostics.get("stagger_known",False)):
        inferred = _infer_unknown_stagger_model(detections,model)
        if inferred is not None:
            model,inferred_linear,inferred_translation,inference = inferred
            logical = np.asarray(model.logical_positions,dtype=np.float64)
            fitted = _refine_registration(
                model,points,inferred_linear,inferred_translation,options,tree=tree,
            )
            inferred_fit_sufficient = (
                fitted is not None
                and _fast_fit_sufficient(fitted,logical.shape[1])
                and (
                    options.expected_period_px is None
                    or _guided_fit_sufficient(
                        fitted,logical.shape[1],options
                    )
                )
            )
            if inferred_fit_sufficient:
                fitted["initializer_source"] = "image_inferred_lattice"
                fitted["correlation_score"] = None
                fitted["search_path"] = "image_inferred_lattice"
                fitted["fallback_used"] = False
                fitted["stagger_inference"] = inference
                return _registration_result(
                    model,detections,fitted,options,False,
                )
            # Keep the inferred representation for the normal guided/global
            # fallback search even when the direct refinement is incomplete.

    # Explicit geometry reuse remains the strongest possible initializer for
    # generic callers.  Feedback's exact-reuse workflow bypasses this function.
    if _compatible_previous(previous,model):
        fitted = _refine_registration(
            model,points,
            np.asarray(previous.affine_linear,dtype=np.float64),
            np.asarray(previous.affine_translation,dtype=np.float64),
            options,tree=tree,
        )
        if fitted is not None:
            fitted["initializer_source"] = "previous"
            fitted["correlation_score"] = None
            fitted["search_path"] = "previous"
            fitted["fallback_used"] = False
            return _registration_result(
                model,detections,fitted,options,True,
            )

    # Explicit detector-space affine geometry, when supplied by a generic
    # caller, skips FFT geometry search.  Feedback no longer constructs this
    # from target rotation/skew.
    if initial_linear is not None:
        linear = np.asarray(initial_linear,dtype=np.float64)
        if linear.shape != (2,2):
            raise ValueError("initial_linear must have shape (2, 2)")
        if initial_translation is None:
            translated = correlation.translation_for(logical,linear)
            if translated is None:
                translation = _initial_translation(logical,points,linear)
                corr_score = None
            else:
                translation,corr_score = translated
        else:
            translation = np.asarray(initial_translation,dtype=np.float64)
            corr_score = None
        if translation.shape != (2,):
            raise ValueError("initial_translation must have shape (2,)")

        translation = _best_period_neighbor_translation(
            logical,points,linear,translation,options,tree,
        )
        fitted = _refine_registration(
            model,points,linear,translation,options,tree=tree,
        )
        if fitted is not None:
            fitted["initializer_source"] = "explicit_linear"
            fitted["correlation_score"] = corr_score
            fitted["search_path"] = "explicit_linear"
            fitted["fallback_used"] = False
            return _registration_result(
                model,detections,fitted,options,False,
            )

    # Build the ordered search plan.  Period guidance is attempted first, but
    # global fallbacks deliberately remove it so a stale calibration or manual
    # value cannot make localization fail outright.
    search_plan = []
    if options.expected_period_px is not None:
        search_plan.append((
            "period_guided",
            options,
            True,
            False,
        ))

    global_options = replace(options,expected_period_px=None)
    if str(options.lattice_candidate_search_mode) == "fast":
        search_plan.append((
            "global_fast",
            global_options,
            False,
            False,
        ))
        search_plan.append((
            "global_full",
            replace(global_options,lattice_candidate_search_mode="full"),
            False,
            True,
        ))
    else:
        search_plan.append((
            "global_full",
            replace(global_options,lattice_candidate_search_mode="full"),
            False,
            True,
        ))

    fallback_best = None
    for pass_index,(search_name,search_options,period_guided,allow_legacy) in enumerate(search_plan):
        fitted = _search_fft_registration(
            detections=detections,
            model=model,
            points=points,
            tree=tree,
            correlation=correlation,
            options=search_options,
            search_name=search_name,
            period_guided=period_guided,
            allow_legacy=allow_legacy,
        )
        if fitted is None:
            continue

        fitted["search_path"] = search_name
        fitted["fallback_used"] = bool(pass_index > 0)
        score_options = options if period_guided else search_options
        score = _fit_score(fitted,score_options)
        if fallback_best is None or score > fallback_best[0]:
            fallback_best = (score,fitted)

        if period_guided:
            if _guided_fit_sufficient(fitted,logical.shape[1],options):
                return _registration_result(
                    model,detections,fitted,options,False,
                )
            continue

        if search_name == "global_fast":
            if _fast_fit_sufficient(fitted,logical.shape[1]):
                return _registration_result(
                    model,detections,fitted,options,False,
                )
            continue

        # Full search is the final numerical fallback.  Return any valid fit it
        # could establish, including intentionally partial localizations.
        return _registration_result(
            model,detections,fitted,options,False,
        )

    if fallback_best is not None:
        fitted = fallback_best[1]
        fitted["fallback_used"] = True
        return _registration_result(
            model,detections,fitted,options,False,
        )

    raise RuntimeError(
        "Could not register the detected spots to a stable 2D lattice; "
        "relax localization parameters or use the full candidate search."
    )

def _guided_fit_sufficient(fitted,target_count,options):
    """Accept a period-guided fit only when it is both complete and plausible."""
    matched = int(fitted["target_indices"].size)
    if matched < int(np.ceil(0.95*int(target_count))):
        return False

    expected = options.expected_period_px
    if expected is None:
        return True
    lengths = np.linalg.norm(np.asarray(fitted["linear"],dtype=np.float64),axis=0)
    return _period_within_tolerance(
        lengths,
        expected,
        max(float(options.period_tolerance_fraction),0.05),
    )

def _fast_fit_sufficient(fitted,target_count):
    """Decide whether the cheap generic search is good enough to stop."""
    matched = int(fitted["target_indices"].size)
    return matched >= int(np.ceil(0.95*int(target_count)))

def _registration_result(
    model,detections,fitted,options,reused_previous,
):
    return _build_result(
        model,detections,fitted,options,reused_previous=reused_previous,
    )

def _compatible_previous(previous,model):
    return (
        previous is not None
        and previous.model.lattice_indices.shape == model.lattice_indices.shape
        and np.array_equal(previous.model.lattice_indices,model.lattice_indices)
        and np.allclose(previous.model.logical_positions,model.logical_positions)
    )

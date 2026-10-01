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
from .finite import _infer_finite_grid_initializer

def register_lattice(
    detections: DetectedSpots,
    model: LatticeModel,
    options: LatticeRegistrationOptions=LatticeRegistrationOptions(),
    *,
    previous: LatticeRegistration | None=None,
    initial_linear: np.ndarray | None=None,
    initial_translation: np.ndarray | None=None,
) -> LatticeRegistration:
    """Register indexed lattice points using finite topology and/or FFT search.

    ``auto`` prefers finite-grid row/column topology because a known finite
    ``Nx x Ny`` target contains information that an infinite Bravais lattice
    does not.  If that topology cannot establish a near-complete fit, the
    existing guided/global FFT search remains available as a fallback.
    ``finite_grid`` and ``fft_lattice`` explicitly force either strategy for
    diagnosis and difficult datasets.
    """
    points = np.asarray(detections.positions_px,dtype=np.float64)
    if points.shape[1] < 3:
        raise RuntimeError(
            "At least three detected spots are required for lattice registration"
        )

    logical = np.asarray(model.logical_positions,dtype=np.float64)
    tree = cKDTree(points.T)
    correlation = _CorrelationWorkspace(detections.processed_image)
    registration_mode = str(options.registration_mode)

    # Explicit geometry reuse remains the strongest initializer.  Feedback's
    # exact-reuse workflow normally bypasses this function entirely.
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

    # Explicit detector-space affine geometry supplied by a generic caller also
    # remains authoritative and skips automatic geometry discovery.
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

    # Finite-grid topology is the normal first choice whenever it is allowed.
    # Unlike FFT basis discovery, it keeps the finite target's row/column
    # indexing canonical and therefore cannot prefer an equivalent ``b + a``
    # primitive basis.  Known stagger is retained; unknown stagger is inferred
    # by the same finite-row analysis.
    finite_candidate = None
    if registration_mode != "fft_lattice":
        inferred = _infer_finite_grid_initializer(detections,model)
        if inferred is not None:
            model,finite_linear,finite_translation,inference = inferred
            logical = np.asarray(model.logical_positions,dtype=np.float64)
            fitted = _refine_registration(
                model,points,finite_linear,finite_translation,options,tree=tree,
            )
            if fitted is not None:
                fitted["initializer_source"] = "finite_grid"
                fitted["correlation_score"] = None
                fitted["search_path"] = "finite_grid"
                fitted["fallback_used"] = False
                fitted["finite_grid_inference"] = inference
                if (
                    registration_mode == "finite_grid"
                    or _fast_fit_sufficient(fitted,logical.shape[1])
                ):
                    return _registration_result(
                        model,detections,fitted,options,False,
                    )
                # A partial but numerically valid finite-topology fit remains a
                # strong fallback.  Auto may still try FFT for a more complete
                # solution, but a weaker periodic-basis fit must not erase this
                # topology-preserving result.
                finite_candidate = (
                    _fit_score(fitted,options),fitted,model,
                )
            # In auto mode retain an image-resolved stagger model if finite
            # topology found one, then let FFT attempt the difficult remainder.
        if registration_mode == "finite_grid":
            raise RuntimeError(
                "Finite-grid registration could not establish the requested "
                "row/column topology. Adjust spot detection or choose Auto/FFT "
                "registration for a periodic-lattice fallback."
            )

    # FFT/Bravais-lattice discovery is now a fallback in Auto mode and remains
    # directly selectable for diagnosis.  Preserve the historical guided ->
    # global-fast -> global-full behavior here so existing difficult/partial
    # datasets still have the broad numerical search available.
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

    fallback_best = finite_candidate
    search_errors = []
    for pass_index,(search_name,search_options,period_guided,allow_legacy) in enumerate(search_plan):
        try:
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
        except RuntimeError as error:
            # A failed periodic search must not erase an already valid finite
            # topology result. Keep trying the remaining FFT passes; if none
            # succeeds, Auto returns the finite candidate below.
            search_errors.append(error)
            continue
        if fitted is None:
            continue

        fitted["search_path"] = search_name
        fitted["fallback_used"] = bool(
            registration_mode == "auto" or pass_index > 0
        )
        score_options = options if period_guided else search_options
        score = _fit_score(fitted,score_options)
        if fallback_best is None or score > fallback_best[0]:
            fallback_best = (score,fitted,model)

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

        # Full FFT search is the final periodic candidate, but Auto may already
        # hold a better finite-topology fit. Compare them instead of returning
        # the periodic result unconditionally.
        continue

    if fallback_best is not None:
        fitted = fallback_best[1]
        result_model = fallback_best[2]
        if fitted.get("search_path") != "finite_grid":
            fitted["fallback_used"] = True
        return _registration_result(
            result_model,detections,fitted,options,False,
        )

    if search_errors:
        raise RuntimeError(
            "Could not register the detected spots to a stable 2D lattice; "
            "adjust localization parameters or try another registration mode. "
            f"Last FFT error: {search_errors[-1]}"
        ) from search_errors[-1]
    raise RuntimeError(
        "Could not register the detected spots to a stable 2D lattice; "
        "adjust localization parameters or try another registration mode."
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
        float(options.period_tolerance_fraction),
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

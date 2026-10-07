"""Execute one lattice-registration search pass.

A search pass turns candidate detector-space lattice bases into translation
initializers, refines them against localized spots, and keeps the best fit.
The caller decides *which* pass to run (guided, fast, or full); this module
implements the common mechanics of a single pass.
"""

from __future__ import annotations

import numpy as np

from .candidates import (
    _basis_orientations,
    _deduplicate_linear_candidates,
    _period_within_tolerance,
    _prior_basis_candidates,
    _representation_affine_variants,
)
from .correlation import (
    _best_period_neighbor_translation,
    _translation_hypotheses,
)
from .fft import _fft_basis_candidates
from .refinement import _excellent_fit,_fit_score,_refine_registration

def _search_fft_registration(
    *,
    detections,
    model,
    points,
    tree,
    correlation,
    options,
    search_name,
    period_guided,
    allow_legacy,
):
    """Run one FFT candidate/correlation/refinement search pass."""
    logical = np.asarray(model.logical_positions,dtype=np.float64)
    representation = model.representation
    candidates = []

    # Generic callers may still provide both period and explicit rotation.  The
    # feedback path intentionally supplies no target-derived rotation.
    for linear in _prior_basis_candidates(options):
        for candidate in _representation_affine_variants(linear,representation):
            candidates.append(("prior",candidate))

    fft_bases = _fft_basis_candidates(
        detections.processed_image,
        options,
        period_guided=bool(period_guided),
        representation=representation,
    )

    for linear in fft_bases:
        for oriented in _basis_orientations(
            linear,options.expected_rotation_deg,
        ):
            for candidate in _representation_affine_variants(oriented,representation):
                if (
                    period_guided
                    and options.expected_period_px is not None
                    and not _period_within_tolerance(
                        np.linalg.norm(candidate,axis=0),
                        options.expected_period_px,
                        max(float(options.period_tolerance_fraction),0.01),
                    )
                ):
                    continue
                candidates.append(("fft",candidate))

    candidates = _deduplicate_linear_candidates(candidates)
    if not candidates:
        return None

    initializers = []
    for source,linear in candidates:
        translated = correlation.translation_for(logical,linear)
        if translated is None:
            continue
        translation,corr_score = translated
        initializers.append((float(corr_score),source,linear,translation))
    initializers.sort(key=lambda item:item[0],reverse=True)
    if not initializers:
        return None

    best = None
    first_pass = min(8,len(initializers))
    for pass_index,(corr_score,source,linear,translation) in enumerate(initializers):
        trial_translation = _best_period_neighbor_translation(
            logical,points,linear,translation,options,tree,
        )
        fitted = _refine_registration(
            model,points,linear,trial_translation,options,tree=tree,
        )
        if fitted is not None:
            fitted["initializer_source"] = "%s:%s" % (search_name,source)
            fitted["correlation_score"] = float(corr_score)
            score = _fit_score(fitted,options)
            if best is None or score > best[0]:
                best = (score,fitted)
            if _excellent_fit(fitted,logical.shape[1]):
                break

        if pass_index + 1 == first_pass and best is not None:
            matched = int(best[1]["target_indices"].size)
            if matched >= int(np.ceil(0.95*logical.shape[1])):
                break

    if best is None and allow_legacy:
        for source,linear in candidates[:8]:
            for translation in _translation_hypotheses(
                logical,points,linear,options,max_hypotheses=1,
            ):
                fitted = _refine_registration(
                    model,points,linear,translation,options,tree=tree,
                )
                if fitted is None:
                    continue
                fitted["initializer_source"] = (
                    "%s:%s_legacy_translation" % (search_name,source)
                )
                fitted["correlation_score"] = None
                score = _fit_score(fitted,options)
                if best is None or score > best[0]:
                    best = (score,fitted)

    return None if best is None else best[1]

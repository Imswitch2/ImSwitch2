"""Discover candidate detector-space lattice bases in the frequency domain.

The FFT stage proposes primitive image-space basis vectors.  It does not own
finite-lattice indexing, translation, matching, or affine refinement; those
remain separate stages of registration.
"""

from __future__ import annotations

import itertools

import numpy as np
from scipy.ndimage import gaussian_filter,maximum_filter

from .candidates import (
    _representation_period_compatible,
    _representation_period_prior_score,
)

def _fft_basis_candidates(
    image,options,*,period_guided=False,representation=None,
):
    array = np.asarray(image,dtype=np.float64)
    h,w = array.shape

    if h < 4 or w < 4:
        raise RuntimeError(
            "Localization crop is too small for lattice estimation"
        )

    window = np.hanning(h)[:,None] * np.hanning(w)[None,:]
    centered = array - float(np.mean(array))

    spectrum = np.abs(
        np.fft.fftshift(
            np.fft.fft2(centered * window)
        )
    )
    spectrum = gaussian_filter(spectrum,0.8)

    cy,cx = h//2,w//2

    yy,xx = np.meshgrid(
        np.arange(h),
        np.arange(w),
        indexing="ij",
    )

    radius = np.sqrt(
        (yy-cy)**2 + (xx-cx)**2
    )

    spectrum[
        radius
        < float(options.fft_exclude_fraction) * min(h,w)
    ] = 0.0

    local = maximum_filter(
        spectrum,
        size=3,
        mode="nearest",
    )

    mask = (
        (spectrum == local)
        & (spectrum > 0)
    )

    py,px = np.nonzero(mask)
    values = spectrum[py,px]

    order = np.argsort(values)[::-1]

    vectors = []
    strengths = []

    for idx in order:
        fx = float(px[idx] - cx) / float(w)
        fy = float(py[idx] - cy) / float(h)

        # Reciprocal-space conjugate peaks are equivalent here.
        # Keep one canonical half-plane representation.
        if fx < 0 or (
            np.isclose(fx,0.0)
            and fy < 0
        ):
            fx,fy = -fx,-fy

        vector = np.array(
            [fx,fy],
            dtype=np.float64,
        )

        if np.linalg.norm(vector) <= 1e-12:
            continue

        if any(
            np.linalg.norm(vector-v)
            < 0.5/min(h,w)
            for v in vectors
        ):
            continue

        vectors.append(vector)
        strengths.append(float(values[idx]))

        if len(vectors) >= int(options.fft_peak_count):
            break

    if len(vectors) < 2:
        raise RuntimeError(
            "Could not detect enough reciprocal-lattice peaks"
        )

    vectors_array = np.column_stack(vectors)
    strengths_array = np.asarray(
        strengths,
        dtype=np.float64,
    )

    max_strength = float(
        np.max(strengths_array)
    )

    candidates = []

    for i,j in itertools.combinations(
        range(len(vectors)),
        2,
    ):
        reciprocal = np.column_stack([
            vectors[i],
            vectors[j],
        ])

        determinant = float(
            np.linalg.det(reciprocal)
        )

        if abs(determinant) < 1e-6:
            continue

        linear = np.linalg.inv(
            reciprocal
        ).T

        lengths = np.linalg.norm(
            linear,
            axis=0,
        )

        if (
            np.any(lengths < 2.0)
            or np.any(
                lengths > 1.5*max(h,w)
            )
        ):
            continue

        prior = _representation_period_prior_score(
            linear,
            representation,
            options.expected_period_px,
            options.period_tolerance_fraction,
        )

        strength = (
            strengths[i]
            + strengths[j]
        )

        lattice_consistency = (
            _reciprocal_lattice_consistency(
                reciprocal,
                vectors_array,
                strengths_array,
            )
        )

        if max_strength > 0.0:
            normalized_strength = (
                strength
                / (2.0 * max_strength)
            )
        else:
            normalized_strength = 0.0

        fast_score = (
            lattice_consistency
            * normalized_strength
        )

        candidates.append(
            (
                prior,
                strength,
                fast_score,
                lattice_consistency,
                linear,
                tuple(float(value) for value in lengths),
            )
        )

    if not candidates:
        raise RuntimeError(
            "No valid pair of reciprocal-lattice peaks was found"
        )

    if period_guided and options.expected_period_px is not None:
        tolerance = max(float(options.period_tolerance_fraction),0.01)
        candidates = [
            candidate
            for candidate in candidates
            if _representation_period_compatible(
                candidate[4],
                representation,
                options.expected_period_px,
                tolerance,
            )
        ]
        if not candidates:
            return []

    mode = str(
        options.lattice_candidate_search_mode
    )

    if mode == "fast":
        # Prefer bases that both:
        #
        # 1. contain strong FFT peaks, and
        # 2. explain the remaining FFT peaks as integer combinations
        #    of the same reciprocal lattice.
        #
        # Three bases normally become at most six real-space geometry
        # candidates after orientation handling.
        candidates.sort(
            key=lambda item:(
                item[0],
                item[2],
                item[1],
            ),
            reverse=True,
        )

        selected = _select_fast_lattice_candidates(
            candidates,
            max_candidates=3,
            min_best_score=(0.70 if period_guided else 0.90),
            min_consistency=(0.70 if period_guided else 0.95),
        )

    else:
        # Original broad search behavior.
        candidates.sort(
            key=lambda item:(
                item[0],
                item[1],
            ),
            reverse=True,
        )

        limit = 20

        selected = candidates[:limit]


    return [
        item[4]
        for item in selected
    ]

def _select_fast_lattice_candidates(
    candidates,
    max_candidates=3,
    min_best_score=0.90,
    min_consistency=0.95,
):
    """Select only FFT lattice candidates that remain competitive.

    A clearly dominant candidate is used alone.  When several candidates have
    similar FFT lattice scores, retain more of them so the real-space template
    correlation can resolve the ambiguity.
    """
    if not candidates:
        return []

    candidates = candidates[:int(max_candidates)]

    if len(candidates) == 1:
        return candidates

    best_score = float(candidates[0][2])
    best_consistency = float(candidates[0][3])

    # Do not aggressively prune when the Fourier lattice itself is uncertain.
    if (
        best_score < float(min_best_score)
        or best_consistency < float(min_consistency)
    ):
        return candidates

    # Keep candidates whose score remains close to the best candidate.
    #
    # With a 90% threshold:
    #
    #   0.995, 0.846, 0.842 -> 1 candidate
    #   0.960, 0.940, 0.710 -> 2 candidates
    #   0.910, 0.890, 0.870 -> 3 candidates
    relative_threshold = 0.90 * best_score

    selected = [
        candidate
        for candidate in candidates
        if float(candidate[2]) >= relative_threshold
    ]

    return selected or [candidates[0]]

def _reciprocal_lattice_consistency(
    reciprocal,
    vectors,
    strengths,
):
    """Score how well one reciprocal basis explains the detected FFT peaks.

    A genuine reciprocal-lattice basis should express the other Fourier peaks
    approximately as integer combinations h*g1 + k*g2.

    The score is weighted by FFT peak strength and lies approximately in
    [0, 1], with values near one indicating strong lattice consistency.
    """
    reciprocal = np.asarray(
        reciprocal,
        dtype=np.float64,
    )

    vectors = np.asarray(
        vectors,
        dtype=np.float64,
    )

    strengths = np.asarray(
        strengths,
        dtype=np.float64,
    )

    try:
        coefficients = np.linalg.solve(
            reciprocal,
            vectors,
        )
    except np.linalg.LinAlgError:
        return 0.0

    nearest_integer = np.rint(
        coefficients
    )

    integer_error = np.sqrt(
        np.sum(
            (
                coefficients
                - nearest_integer
            ) ** 2,
            axis=0,
        )
    )

    # Approximately ±0.15 reciprocal-basis units around an integer
    # combination receives strong support. The smooth Gaussian score avoids
    # introducing another hard threshold.
    sigma = 0.15

    support = np.exp(
        -0.5
        * (
            integer_error
            / sigma
        ) ** 2
    )

    weight_sum = float(
        np.sum(strengths)
    )

    if weight_sum <= 0.0:
        return float(
            np.mean(support)
        )

    return float(
        np.sum(
            support * strengths
        )
        / weight_sum
    )

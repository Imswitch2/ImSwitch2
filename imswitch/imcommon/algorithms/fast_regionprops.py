"""Vectorized region-property features (skimage-compatible) via a single scatter pass.

``skimage.measure.regionprops_table`` iterates region-by-region in Python, so
its cost scales with (#regions x #images). For features that are pure per-label
reductions (sum / count / max / min) or closed-form functions of one, the whole
table can instead be computed with a single vectorized scatter pass over the
foreground pixels using ``np.bincount`` / ``np.maximum.at`` — a typical 10–80x
speedup that grows with the number of regions.

Provenance
----------
The core (:func:`regionprops_table_fast` and its helpers, down to the
``ImSwitch2 extensions`` divider) is vendored **verbatim** from

    fast-regionprops — Martin Weigert — BSD-3-Clause
    https://github.com/maweigert/fast-regionprops
    commit 89c6741 (2026-06-18)

It is vendored rather than taken as a PyPI dependency because it is a single
self-contained module (numpy + scipy only, both already core deps) still at
``0.1.0``; vendoring pins the exact code and avoids a pre-1.0 supply-chain
dependency. To update, re-copy ``src/fast_regionprops/core.py`` over the
vendored region and re-run ``test_fast_regionprops.py``. Only two local edits
were made to the vendored region: this docstring, and making the
``scipy.special.gamma`` import function-local to honour the
``imcommon.algorithms`` "keep heavy deps lazy" convention.

The ``ImSwitch2 extensions`` section below adds the shape descriptors
(``eccentricity``, ``axis_major_length``, ``axis_minor_length``, ``orientation``)
that fast-regionprops intentionally leaves to the caller — they are pure
closed-form functions of the supported ``inertia_tensor`` (exactly how skimage
derives them internally).

Supported properties
---------------------
``label, area, centroid, bbox, equivalent_diameter_area, intensity_mean,
intensity_min, intensity_max, intensity_std, inertia_tensor, border_dist``.

Properties that need a convex hull / perimeter / topology (``solidity``,
``perimeter``, ``euler_number``, ...) are intentionally out of scope: they are
not simple reductions and belong on skimage.
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np

__all__ = [
    "ALL_PROPERTIES",
    "DEFAULT_PROPERTIES",
    "regionprops_table_fast",
    "region_shape_descriptors",
]

ALL_PROPERTIES: tuple[str, ...] = (
    "label",
    "area",
    "centroid",
    "bbox",
    "equivalent_diameter_area",
    "intensity_mean",
    "intensity_min",
    "intensity_max",
    "intensity_std",
    "inertia_tensor",
    "border_dist",
)

DEFAULT_PROPERTIES: tuple[str, ...] = (
    "label",
    "bbox",
)

_INTENSITY_PROPS = frozenset(
    {"intensity_mean", "intensity_min", "intensity_max", "intensity_std"}
)


def _border_dist_field(spatial_shape: tuple[int, ...], cutoff: int) -> np.ndarray:
    """Field that is 1 at the (last-two-axes) border, fading to 0 ``cutoff`` px in."""
    cutoff = int(cutoff)
    if cutoff <= 0:
        raise ValueError("border_dist_cutoff must be a positive integer")
    ndim = len(spatial_shape)
    border = np.ones(spatial_shape, dtype=np.float32)
    for axis, size in enumerate(spatial_shape):
        if axis < ndim - 2:  # only the last two dims, as in the original
            continue
        band = (np.arange(cutoff, dtype=np.float32) / cutoff)[:size]
        lo = [slice(None)] * ndim
        lo[axis] = slice(0, cutoff)
        border[tuple(lo)] = np.minimum(
            border[tuple(lo)], band[(...,) + (None,) * (ndim - axis - 1)]
        )
        hi = [slice(None)] * ndim
        hi[axis] = slice(max(0, size - cutoff), size)
        border[tuple(hi)] = np.minimum(
            border[tuple(hi)], band[::-1][(...,) + (None,) * (ndim - axis - 1)]
        )
    return 1.0 - border


def _equivalent_diameter_area(area: np.ndarray, ndim: int) -> np.ndarray:
    """Diameter of an nd ball with the same volume/area (skimage convention)."""
    from scipy.special import gamma  # lazy: keep module import cheap

    return 2.0 * (area * gamma(ndim / 2 + 1) / np.pi ** (ndim / 2)) ** (1.0 / ndim)


def _validate_properties(properties, intensity_image) -> tuple[str, ...]:
    properties = tuple(properties)
    # keep input order, drop duplicates for the report
    unsupported = [p for p in dict.fromkeys(properties) if p not in ALL_PROPERTIES]
    if unsupported:
        supported = [p for p in dict.fromkeys(properties) if p in ALL_PROPERTIES]
        raise ValueError(
            f"Unsupported propert{'y' if len(unsupported) == 1 else 'ies'}: "
            f"{unsupported}. "
            f"Supported here: {supported or '(none of the requested)'}. "
            f"fast-regionprops only supports the reduction-only set "
            f"{list(ALL_PROPERTIES)}; for anything else "
            f"(e.g. solidity, perimeter, euler_number) use "
            f"skimage.measure.regionprops_table."
        )
    if intensity_image is None and (_INTENSITY_PROPS & set(properties)):
        raise ValueError(
            "intensity_image is required for intensity_* properties: "
            f"{sorted(_INTENSITY_PROPS & set(properties))}"
        )
    return properties


def _reject_unsupported_args(cache, extra_properties, spacing) -> None:
    """Args accepted to mirror ``regionprops_table`` but not (yet) supported."""
    if extra_properties is not None:
        raise NotImplementedError(
            "extra_properties is not supported by fast-regionprops "
            "(it only computes vectorized reductions). "
            "Use skimage.measure.regionprops_table for custom callables."
        )
    if spacing is not None:
        raise NotImplementedError(
            "spacing (anisotropic pixel size) is not supported yet by "
            "fast-regionprops. Use skimage.measure.regionprops_table."
        )
    # `cache` has no meaning here (single pass, nothing cached); accepted & ignored.


def _validate_label_image(label_image: np.ndarray) -> None:
    """Match skimage's strictness for ambiguous label-image dtypes."""
    if label_image.dtype == bool:
        raise TypeError(
            "Non-integer image types are ambiguous: use skimage.measure.label to "
            "label the connected components of label_image, or "
            "label_image.astype(np.uint8) to interpret the True values as a "
            "single label."
        )
    if not np.issubdtype(label_image.dtype, np.integer):
        raise TypeError("Non-integer label_image types are ambiguous")


def _validate_intensity_image(
    intensity_image: np.ndarray | None, spatial: tuple[int, ...]
) -> tuple[np.ndarray | None, bool]:
    if intensity_image is None:
        return None, False

    intensity_image = np.asarray(intensity_image)
    if intensity_image.shape == spatial:
        return intensity_image, False
    if (
        intensity_image.ndim == len(spatial) + 1
        and intensity_image.shape[:-1] == spatial
    ):
        return intensity_image, True
    raise ValueError(
        "intensity_image must have the same shape as label_image, or an extra "
        "trailing channel axis"
    )


def _bincount_values(inv: np.ndarray, values: np.ndarray, K: int) -> np.ndarray:
    if values.ndim == 1:
        return np.bincount(inv, weights=values, minlength=K)
    return np.stack(
        [
            np.bincount(inv, weights=values[:, channel], minlength=K)
            for channel in range(values.shape[1])
        ],
        axis=1,
    )


def _add_maybe_multichannel(
    out: OrderedDict[str, np.ndarray],
    name: str,
    values: np.ndarray,
    separator: str,
) -> None:
    if values.ndim == 1:
        out[name] = values
    else:
        for channel in range(values.shape[1]):
            out[f"{name}{separator}{channel}"] = values[:, channel]


def _compute(
    label_image: np.ndarray,
    intensity_image: np.ndarray | None,
    properties: tuple[str, ...],
    border_dist_cutoff: int,
    separator: str,
) -> OrderedDict[str, np.ndarray]:
    """Core scatter pass over the foreground pixels of a single 2D/3D image."""
    sep = separator
    label_image = np.asarray(label_image)
    _validate_label_image(label_image)
    ndim = label_image.ndim
    if ndim not in (2, 3):
        raise ValueError(f"Only 2D or 3D images are supported (got ndim={ndim})")

    spatial = label_image.shape
    need_intensity = bool(_INTENSITY_PROPS & set(properties))
    intensity_image, is_multichannel_intensity = _validate_intensity_image(
        intensity_image, spatial
    )

    # Foreground coordinate tuple: same result as np.nonzero(label_image > 0) but
    # ~1.7x faster (one flat index pass, then coords built only for foreground),
    # and avoids the full-volume np.indices(spatial)[fg] for sparse / 3D images.
    fg = np.unravel_index(np.flatnonzero(label_image > 0), spatial)
    g_label = label_image[fg].astype(np.int64)

    # per-pixel coordinates; shift by global min for numerical stability
    coords_pix = np.stack(fg, axis=1).astype(np.float64)
    if coords_pix.size:
        offset = coords_pix.min(axis=0)
        coords_pix -= offset
    else:
        offset = np.zeros(ndim)

    # Relabel original (possibly gappy) labels to a dense 0..K-1 range:
    # `inv` is each pixel's compact id (the scatter index for bincount/maximum.at),
    # `uniq` maps compact id -> original label (sorted, so output rows are
    # ascending-label, matching skimage).
    uniq, inv = np.unique(g_label, return_inverse=True)
    inv = inv.astype(np.intp, copy=False)
    K = len(uniq)

    out: OrderedDict[str, np.ndarray] = OrderedDict()
    out_labels = uniq.astype(np.int64)

    area = np.bincount(inv, minlength=K).astype(np.float64)
    S = np.stack(
        [np.bincount(inv, weights=coords_pix[:, d], minlength=K) for d in range(ndim)],
        axis=1,
    )
    centroid = np.zeros((K, ndim)) if K == 0 else S / area[:, None]

    inten = (
        intensity_image[fg].astype(np.float64)
        if (need_intensity and intensity_image is not None)
        else None
    )

    for p in properties:
        if p == "label":
            out["label"] = out_labels
        elif p == "area":
            out["area"] = area.copy()
        elif p == "centroid":
            for d in range(ndim):
                out[f"centroid{sep}{d}"] = centroid[:, d] + offset[d]
        elif p == "bbox":
            mins = np.full((K, ndim), 0, dtype=np.intp)
            maxs = np.full((K, ndim), 0, dtype=np.intp)
            for d in range(ndim):
                coord = fg[d].astype(np.intp, copy=False)
                lo = np.full(K, np.iinfo(np.intp).max, dtype=np.intp)
                hi = np.full(K, -1, dtype=np.intp)
                np.minimum.at(lo, inv, coord)
                np.maximum.at(hi, inv, coord)
                mins[:, d] = lo
                maxs[:, d] = hi
            for d in range(ndim):  # skimage bbox: mins..., then maxs+1
                out[f"bbox{sep}{d}"] = mins[:, d]
            for d in range(ndim):
                out[f"bbox{sep}{ndim + d}"] = maxs[:, d] + 1
        elif p == "equivalent_diameter_area":
            out["equivalent_diameter_area"] = _equivalent_diameter_area(area, ndim)
        elif p == "intensity_mean":
            divisor = area[:, None] if is_multichannel_intensity else area
            mean = _bincount_values(inv, inten, K) / divisor
            _add_maybe_multichannel(out, "intensity_mean", mean, sep)
        elif p == "intensity_std":
            s1 = _bincount_values(inv, inten, K)
            s2 = _bincount_values(inv, inten * inten, K)
            divisor = area[:, None] if is_multichannel_intensity else area
            var = s2 / divisor - (s1 / divisor) ** 2
            std = np.sqrt(np.clip(var, 0, None))
            _add_maybe_multichannel(out, "intensity_std", std, sep)
        elif p == "intensity_min":
            mn = (
                np.full((K, inten.shape[1]), np.inf)
                if is_multichannel_intensity
                else np.full(K, np.inf)
            )
            np.minimum.at(mn, inv, inten)
            _add_maybe_multichannel(out, "intensity_min", mn, sep)
        elif p == "intensity_max":
            mx = (
                np.full((K, inten.shape[1]), -np.inf)
                if is_multichannel_intensity
                else np.full(K, -np.inf)
            )
            np.maximum.at(mx, inv, inten)
            _add_maybe_multichannel(out, "intensity_max", mx, sep)
        elif p == "inertia_tensor":
            M = np.zeros((K, ndim, ndim))
            for d in range(ndim):
                for e in range(d, ndim):
                    Sde = np.bincount(
                        inv,
                        weights=coords_pix[:, d] * coords_pix[:, e],
                        minlength=K,
                    )
                    m = (Sde / area - centroid[:, d] * centroid[:, e]) if K else 0.0
                    M[:, d, e] = m
                    M[:, e, d] = m
            # skimage inertia tensor: off-diag = -M_de ; diag = trace(M) - M_dd
            trace = np.einsum("kii->k", M)
            T = -M
            for d in range(ndim):
                T[:, d, d] = trace - M[:, d, d]
            for i in range(ndim):
                for j in range(ndim):
                    out[f"inertia_tensor{sep}{i}{sep}{j}"] = T[:, i, j]
        elif p == "border_dist":
            field = _border_dist_field(spatial, border_dist_cutoff)
            bd = np.zeros(K)
            np.maximum.at(bd, inv, field[fg])
            out["border_dist"] = bd
        else:  # pragma: no cover - guarded by _validate_properties
            raise ValueError(f"Unhandled property {p!r}")

    return out


def regionprops_table_fast(
    label_image: np.ndarray,
    intensity_image: np.ndarray | None = None,
    properties=DEFAULT_PROPERTIES,
    *,
    cache: bool = True,
    separator: str = "-",
    extra_properties=None,
    spacing=None,
    border_dist_cutoff: int = 5,
) -> dict[str, np.ndarray]:
    """Fast region-property table for a single 2D or 3D label image.

    Drop-in for ``skimage.measure.regionprops_table`` for the reduction-only
    properties listed in :data:`ALL_PROPERTIES`. Returns a dict of 1D arrays
    keyed with skimage-style names (``centroid-0``, ``inertia_tensor-0-1`` ...),
    one row per label, ordered by ascending label.

    The signature mirrors ``regionprops_table``. ``cache`` is accepted but has
    no effect (a single scatter pass caches nothing). ``extra_properties`` and
    ``spacing`` are accepted for signature compatibility but raise
    ``NotImplementedError`` if used.

    Args:
        label_image: Integer label image; 0 is background.
        intensity_image: Same-shape intensity image, optionally with an extra
            trailing channel axis (required for intensity_*).
        properties: Properties to compute (subset of :data:`ALL_PROPERTIES`).
        cache: Accepted for compatibility; ignored.
        separator: Separator between a property name and its index in column keys.
        extra_properties: Not supported (raises if not None).
        spacing: Not supported (raises if not None).
        border_dist_cutoff: Fade distance (px) for ``border_dist``.

    Returns:
        dict mapping column name -> ``np.ndarray`` of shape ``(n_labels,)``.
    """
    _reject_unsupported_args(cache, extra_properties, spacing)
    properties = _validate_properties(properties, intensity_image)
    return dict(
        _compute(
            label_image,
            intensity_image,
            properties,
            border_dist_cutoff,
            separator=separator,
        )
    )


# =============================================================================
# ImSwitch2 extensions
# -----------------------------------------------------------------------------
# Shape descriptors that fast-regionprops leaves to the caller. Each is a pure
# closed-form function of the supported 2D ``inertia_tensor``, computed the same
# way skimage's RegionProperties does (see skimage.measure._regionprops). Kept
# here so callers get eccentricity / axis lengths / orientation without a
# skimage regionprops loop. Validated against skimage in test_fast_regionprops.
# =============================================================================

_DERIVED_2D_PROPS: tuple[str, ...] = (
    "eccentricity",
    "axis_major_length",
    "axis_minor_length",
    "orientation",
)


def _inertia_eigvals_2d(
    t00: np.ndarray, t01: np.ndarray, t11: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Eigenvalues (descending, clipped at 0) of a batch of symmetric 2x2 tensors.

    Closed form; matches ``np.linalg.eigvalsh`` sorted ``reverse=True`` with the
    small-negative clip skimage applies for floating-point safety.
    """
    half_trace = (t00 + t11) / 2.0
    disc = np.sqrt(np.maximum(((t00 - t11) / 2.0) ** 2 + t01 ** 2, 0.0))
    l1 = np.clip(half_trace + disc, 0.0, None)  # larger eigenvalue
    l2 = np.clip(half_trace - disc, 0.0, None)  # smaller eigenvalue
    return l1, l2


def region_shape_descriptors(
    table: dict[str, np.ndarray], *, separator: str = "-"
) -> dict[str, np.ndarray]:
    """Derive 2D shape descriptors from a fast-regionprops ``inertia_tensor``.

    Args:
        table: Output of :func:`regionprops_table_fast` computed with the
            ``inertia_tensor`` property (2D image).
        separator: The separator used when the table was built.

    Returns:
        dict with ``eccentricity``, ``axis_major_length``, ``axis_minor_length``,
        and ``orientation`` arrays, matching skimage's definitions and units.
    """
    sep = separator
    try:
        t00 = np.asarray(table[f"inertia_tensor{sep}0{sep}0"], dtype=np.float64)
        t01 = np.asarray(table[f"inertia_tensor{sep}0{sep}1"], dtype=np.float64)
        t11 = np.asarray(table[f"inertia_tensor{sep}1{sep}1"], dtype=np.float64)
    except KeyError as e:
        raise KeyError(
            "region_shape_descriptors needs a 2D inertia_tensor in the table; "
            "call regionprops_table_fast(..., properties=(..., 'inertia_tensor'))."
        ) from e

    l1, l2 = _inertia_eigvals_2d(t00, t01, t11)

    major = 4.0 * np.sqrt(l1)
    minor = 4.0 * np.sqrt(l2)

    with np.errstate(divide="ignore", invalid="ignore"):
        eccentricity = np.sqrt(1.0 - l2 / l1)
    eccentricity = np.where(l1 > 0, eccentricity, 0.0)

    # skimage orientation: 0.5*atan2(-2b, c-a); a==c edge -> ±pi/4 by sign of b.
    a, b, c = t00, t01, t11
    orientation = 0.5 * np.arctan2(-2.0 * b, c - a)
    orientation = np.where(
        a - c == 0, np.where(b < 0, -np.pi / 4.0, np.pi / 4.0), orientation
    )

    return {
        "eccentricity": eccentricity,
        "axis_major_length": major,
        "axis_minor_length": minor,
        "orientation": orientation,
    }

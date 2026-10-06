"""Grid localization used by uLenses and transform-calibration workflows.

Two APIs deliberately coexist:

``localizer``
    The historical axis-aligned 1-D periodic localizer used by
    :class:`ULensesController`.  It is kept unchanged for compatibility.

``localize_grid``
    A geometry-first 2-D grid localizer.  It estimates a finite tilted grid,
    including optional alternating-row/column stagger, without performing the
    high-accuracy focus correspondence required by transform calibration.
    When ``n_cols`` and ``n_rows`` are supplied they are treated as a strong
    finite-topology prior; otherwise the finite extent is inferred from the
    detected grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks
from scipy.spatial import cKDTree

__all__ = [
    "LocalizationResult",
    "GridLocalizationResult",
    "localizer",
    "localize_grid",
    "refine_grid_foci",
]


@dataclass(frozen=True)
class LocalizationResult:
    """Legacy axis-aligned uLens localization result."""

    xp: float
    xo: float
    yp: float
    yo: float
    nx_c: int
    ny_c: int
    num_cols: int
    num_rows: int


@dataclass(frozen=True)
class GridLocalizationResult:
    """Finite 2-D grid geometry in image ``(row, col)`` coordinates.

    ``basis_x_rc`` advances one logical column and ``basis_y_rc`` advances one
    logical row.  ``stagger_axis`` is ``"rows"`` when odd rows are shifted by
    ``stagger_fraction * basis_x_rc``, ``"columns"`` when odd columns are
    shifted by ``stagger_fraction * basis_y_rc``, or ``None``.

    ``grid_points_rc`` and ``logical_indices`` are row-major and therefore form
    the stable finite-grid identity used by later correspondence/refinement.
    Coarse ``detected_points_rc`` are retained only as diagnostics; callers
    needing precise focus coordinates should use :func:`refine_grid_foci`.
    """

    origin_rc: np.ndarray
    basis_x_rc: np.ndarray
    basis_y_rc: np.ndarray
    n_cols: int
    n_rows: int
    stagger_axis: str | None
    stagger_fraction: float
    grid_points_rc: np.ndarray
    logical_indices: np.ndarray
    detected_points_rc: np.ndarray
    num_cols: int
    num_rows: int
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        origin = np.asarray(self.origin_rc, dtype=np.float64).reshape(2)
        basis_x = np.asarray(self.basis_x_rc, dtype=np.float64).reshape(2)
        basis_y = np.asarray(self.basis_y_rc, dtype=np.float64).reshape(2)
        points = np.asarray(self.grid_points_rc, dtype=np.float64)
        indices = np.asarray(self.logical_indices, dtype=np.int64)
        detections = np.asarray(self.detected_points_rc, dtype=np.float64)
        if points.shape != (int(self.n_rows) * int(self.n_cols), 2):
            raise ValueError(
                "grid_points_rc must contain exactly n_rows * n_cols points"
            )
        if indices.shape != points.shape:
            raise ValueError("logical_indices must match grid_points_rc shape")
        if detections.ndim != 2 or detections.shape[1] != 2:
            raise ValueError("detected_points_rc must have shape (N, 2)")
        if self.stagger_axis not in (None, "rows", "columns"):
            raise ValueError("stagger_axis must be None, 'rows', or 'columns'")
        object.__setattr__(self, "origin_rc", origin)
        object.__setattr__(self, "basis_x_rc", basis_x)
        object.__setattr__(self, "basis_y_rc", basis_y)
        object.__setattr__(self, "grid_points_rc", points)
        object.__setattr__(self, "logical_indices", indices)
        object.__setattr__(self, "detected_points_rc", detections)

    @property
    def x_period_px(self) -> float:
        return float(np.linalg.norm(self.basis_x_rc))

    @property
    def y_period_px(self) -> float:
        return float(np.linalg.norm(self.basis_y_rc))

    @property
    def angle_deg(self) -> float:
        """Logical X-axis angle from image-right, positive toward image-down."""
        return float(np.degrees(np.arctan2(self.basis_x_rc[0], self.basis_x_rc[1])))

    @property
    def shape(self) -> tuple[int, int]:
        return int(self.n_rows), int(self.n_cols)


# ---------------------------------------------------------------------------
# New geometry-first grid localizer
# ---------------------------------------------------------------------------


def localize_grid(
    img_data: np.ndarray,
    *,
    xp_guess: float = 10.0,
    yp_guess: float = 10.0,
    n_cols: int | None = None,
    n_rows: int | None = None,
    threshold_rel: float = 0.10,
    gaussian_sigma: float = 1.0,
    min_distance_fraction: float = 0.35,
    stagger_mode: str = "auto",
    stagger_axis: str = "auto",
    stagger_fraction: float = 0.5,
) -> GridLocalizationResult:
    """Estimate finite tilted-grid geometry from a 2-D image or 3-D stack.

    Supplying both ``n_cols`` and ``n_rows`` activates finite-topology mode and
    those counts are used directly.  With both omitted, an Auto pass infers the
    number of lines and points per line from the detected lattice.  Supplying
    only one count is rejected because it weakens rather than clarifies the
    finite-grid topology.

    The localizer intentionally stops at *global grid geometry*.  Coarse spots
    are used to infer the lattice, but precise per-focus refinement/pairing is a
    separate stage (:func:`refine_grid_foci`).
    """
    xp_guess = float(xp_guess)
    yp_guess = float(yp_guess)
    if xp_guess <= 0 or yp_guess <= 0:
        raise ValueError("grid period guesses must be positive")
    if (n_cols is None) != (n_rows is None):
        raise ValueError("n_cols and n_rows must be supplied together")
    if n_cols is not None:
        n_cols = int(n_cols)
        n_rows = int(n_rows)
        if n_cols < 2 or n_rows < 2:
            raise ValueError("finite grid counts must both be at least 2")
    threshold_rel = float(threshold_rel)
    if not 0.0 <= threshold_rel <= 1.0:
        raise ValueError("threshold_rel must be between 0 and 1")
    gaussian_sigma = float(gaussian_sigma)
    if gaussian_sigma < 0:
        raise ValueError("gaussian_sigma must be non-negative")
    min_distance_fraction = float(min_distance_fraction)
    if not 0.05 <= min_distance_fraction <= 1.0:
        raise ValueError("min_distance_fraction must be within [0.05, 1]")
    stagger_mode = str(stagger_mode).lower()
    if stagger_mode not in ("auto", "none", "manual"):
        raise ValueError("stagger_mode must be 'auto', 'none', or 'manual'")
    stagger_axis = str(stagger_axis).lower()
    if stagger_axis not in ("auto", "rows", "columns"):
        raise ValueError("stagger_axis must be 'auto', 'rows', or 'columns'")
    stagger_fraction = float(stagger_fraction)
    if not -1.0 <= stagger_fraction <= 1.0:
        raise ValueError("stagger_fraction must be within [-1, 1]")
    if stagger_mode == "manual" and stagger_axis == "auto":
        raise ValueError("manual stagger requires stagger_axis rows or columns")

    image = _as_grid_image(img_data)
    height, width = image.shape
    expected_n = None if n_cols is None else int(n_cols * n_rows)
    detected = _detect_grid_spots(
        image,
        xp_guess=xp_guess,
        yp_guess=yp_guess,
        threshold_rel=threshold_rel,
        gaussian_sigma=gaussian_sigma,
        min_distance_fraction=min_distance_fraction,
        expected_n=expected_n,
    )

    if expected_n is not None and len(detected) != expected_n:
        raise RuntimeError(
            f"Detected {len(detected)} spots but manual finite topology requires "
            f"exactly {expected_n} ({n_rows}x{n_cols}). Adjust grid detection "
            "parameters or use Auto grid size."
        )
    if len(detected) < 4:
        raise RuntimeError("At least four grid spots are required")

    if n_cols is None:
        shapes = _infer_grid_shapes(
            detected,
            xp_guess=xp_guess,
            yp_guess=yp_guess,
        )
        if not shapes:
            raise RuntimeError(
                "Auto grid-size inference could not recover a complete finite grid. "
                "Enter n_cols and n_rows manually."
            )
        shape_source = "auto"
    else:
        shapes = [(int(n_cols), int(n_rows))]
        shape_source = "manual"

    candidates = []
    for candidate_cols, candidate_rows in shapes:
        candidate = _fit_finite_grid_geometry(
            detected,
            n_cols=candidate_cols,
            n_rows=candidate_rows,
            xp_guess=xp_guess,
            yp_guess=yp_guess,
            stagger_mode=stagger_mode,
            stagger_axis=stagger_axis,
            stagger_fraction=stagger_fraction,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        if shape_source == "auto":
            hint = "Try manual n_cols/n_rows or adjust grid detection parameters."
        else:
            hint = "Check n_cols/n_rows and grid detection parameters."
        raise RuntimeError(f"Could not fit the finite grid topology. {hint}")

    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    logical = _logical_indices(best["n_cols"], best["n_rows"])
    grid_points = _predict_grid_points(
        best["origin_rc"],
        best["basis_x_rc"],
        best["basis_y_rc"],
        logical,
        best["stagger_axis"],
        best["stagger_fraction"],
    )
    diagnostics = {
        "grid_size_source": shape_source,
        "candidate_shapes": [tuple(map(int, shape)) for shape in shapes],
        "detected_count": int(len(detected)),
        "fit_rms_px": float(best["fit_rms_px"]),
        "period_guess_px": (xp_guess, yp_guess),
        "period_fit_px": (
            float(np.linalg.norm(best["basis_x_rc"])),
            float(np.linalg.norm(best["basis_y_rc"])),
        ),
        "stagger_source": best["stagger_source"],
        "line_inference": best.get("line_inference", {}),
    }
    return GridLocalizationResult(
        origin_rc=best["origin_rc"],
        basis_x_rc=best["basis_x_rc"],
        basis_y_rc=best["basis_y_rc"],
        n_cols=int(best["n_cols"]),
        n_rows=int(best["n_rows"]),
        stagger_axis=best["stagger_axis"],
        stagger_fraction=float(best["stagger_fraction"]),
        grid_points_rc=grid_points,
        logical_indices=logical,
        detected_points_rc=detected,
        num_cols=int(width),
        num_rows=int(height),
        diagnostics=diagnostics,
    )


def refine_grid_foci(
    img_data: np.ndarray,
    grid: GridLocalizationResult,
    *,
    search_radius_px: int = 5,
    gaussian_sigma: float = 1.0,
) -> np.ndarray:
    """Refine each predicted finite-grid site independently to a focus center.

    This is intentionally separate from :func:`localize_grid`: the global grid
    fit is reusable without individual-focus correspondence, while transform
    calibration can request precise, index-preserving focus coordinates.
    """
    image = _as_grid_image(img_data)
    radius = int(search_radius_px)
    if radius < 0:
        raise ValueError("search_radius_px must be non-negative")
    sigma = float(gaussian_sigma)
    if sigma < 0:
        raise ValueError("gaussian_sigma must be non-negative")
    search = gaussian_filter(image.astype(np.float64, copy=False), sigma) if sigma else image

    refined = []
    h, w = image.shape
    for predicted_row, predicted_col in np.asarray(grid.grid_points_rc):
        center_row = int(round(float(predicted_row)))
        center_col = int(round(float(predicted_col)))
        r0 = max(0, center_row - radius)
        r1 = min(h, center_row + radius + 1)
        c0 = max(0, center_col - radius)
        c1 = min(w, center_col + radius + 1)
        if r0 >= r1 or c0 >= c1:
            raise RuntimeError(
                "Predicted grid site lies outside the image; check grid localization"
            )
        patch = search[r0:r1, c0:c1]
        local_flat = int(np.argmax(patch))
        peak_r, peak_c = np.unravel_index(local_flat, patch.shape)
        peak_row = r0 + int(peak_r)
        peak_col = c0 + int(peak_c)

        # Sub-pixel centre-of-mass refinement around the locally selected peak.
        com_radius = max(1, min(radius, 3)) if radius else 0
        rr0 = max(0, peak_row - com_radius)
        rr1 = min(h, peak_row + com_radius + 1)
        cc0 = max(0, peak_col - com_radius)
        cc1 = min(w, peak_col + com_radius + 1)
        local = image[rr0:rr1, cc0:cc1].astype(np.float64, copy=False)
        local = np.clip(local - np.percentile(local, 10), 0.0, None)
        total = float(np.sum(local))
        if total <= 0:
            refined.append((float(peak_row), float(peak_col)))
            continue
        row_coords = np.arange(rr0, rr1, dtype=np.float64)[:, None]
        col_coords = np.arange(cc0, cc1, dtype=np.float64)[None, :]
        refined.append((
            float(np.sum(local * row_coords) / total),
            float(np.sum(local * col_coords) / total),
        ))
    return np.asarray(refined, dtype=np.float64)


def _as_grid_image(img_data: np.ndarray) -> np.ndarray:
    array = np.asarray(img_data)
    if array.ndim == 3:
        array = array.sum(axis=0)
    array = np.squeeze(array)
    if array.ndim != 2:
        raise ValueError(f"Expected 2D or 3D array, got shape {array.shape}")
    array = np.asarray(array, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError("Grid-localization image contains non-finite values")
    return array


def _detect_grid_spots(
    image: np.ndarray,
    *,
    xp_guess: float,
    yp_guess: float,
    threshold_rel: float,
    gaussian_sigma: float,
    min_distance_fraction: float,
    expected_n: int | None,
) -> np.ndarray:
    from skimage.feature import peak_local_max

    signal = np.asarray(image, dtype=np.float64)
    low = float(np.percentile(signal, 1.0))
    high = float(np.percentile(signal, 99.9))
    normalized = np.clip((signal - low) / (high - low + 1e-12), 0.0, 1.0)
    if gaussian_sigma > 0:
        smoothed = gaussian_filter(normalized, gaussian_sigma)
        background_sigma = max(3.0, 0.75 * (xp_guess + yp_guess))
        smoothed = smoothed - gaussian_filter(normalized, background_sigma)
        smoothed = np.maximum(smoothed, 0.0)
    else:
        smoothed = np.maximum(normalized - np.median(normalized), 0.0)
    maximum = float(np.max(smoothed))
    if maximum > 0:
        smoothed = smoothed / maximum

    min_distance = max(
        1,
        int(round(min(xp_guess, yp_guess) * float(min_distance_fraction))),
    )
    kwargs: dict[str, Any] = {
        "min_distance": min_distance,
        "threshold_rel": float(threshold_rel),
        "exclude_border": False,
    }
    if expected_n is not None:
        kwargs["num_peaks"] = int(expected_n)
    else:
        approximate = image.size / max(xp_guess * yp_guess, 1.0)
        kwargs["num_peaks"] = max(8, int(np.ceil(2.0 * approximate)))
    peaks = peak_local_max(smoothed, **kwargs)
    if len(peaks) == 0:
        raise RuntimeError("No grid spots were detected")

    # Coarse sub-pixel refinement.  It deliberately remains lighter than the
    # final transform-correspondence refinement.
    refined = []
    radius = max(1, min(3, min_distance // 2))
    for row, col in peaks:
        r0, r1 = max(0, row - radius), min(image.shape[0], row + radius + 1)
        c0, c1 = max(0, col - radius), min(image.shape[1], col + radius + 1)
        patch = normalized[r0:r1, c0:c1]
        weights = np.clip(patch - np.percentile(patch, 10), 0.0, None)
        total = float(weights.sum())
        if total <= 0:
            refined.append((float(row), float(col)))
            continue
        rr = np.arange(r0, r1, dtype=np.float64)[:, None]
        cc = np.arange(c0, c1, dtype=np.float64)[None, :]
        refined.append((
            float(np.sum(weights * rr) / total),
            float(np.sum(weights * cc) / total),
        ))
    return np.asarray(refined, dtype=np.float64)


def _dominant_neighbor_directions(points: np.ndarray, max_directions: int = 8) -> list[np.ndarray]:
    """Return dominant unoriented local-neighbour directions in ``(row,col)``."""
    points = np.asarray(points, dtype=np.float64)
    count = len(points)
    if count < 4:
        return []
    tree = cKDTree(points)
    k = min(9, count)
    distances, nearest = tree.query(points, k=k)
    if k <= 1:
        return []
    nearest_scale = float(np.median(distances[:, 1]))
    if not np.isfinite(nearest_scale) or nearest_scale <= 0:
        return []

    angles: list[float] = []
    weights: list[float] = []
    for rank in range(1, k):
        valid = np.isfinite(distances[:, rank]) & (distances[:, rank] <= 1.8 * nearest_scale)
        source = np.flatnonzero(valid)
        if source.size == 0:
            continue
        vectors = points[nearest[source, rank]] - points[source]
        angle = np.mod(np.arctan2(vectors[:, 0], vectors[:, 1]), np.pi)
        angles.extend(angle.tolist())
        weights.extend((1.0 / np.maximum(distances[source, rank], 1e-9)).tolist())
    if not angles:
        return []

    bins = 180
    histogram, edges = np.histogram(
        np.asarray(angles), bins=bins, range=(0.0, np.pi), weights=np.asarray(weights)
    )
    histogram = gaussian_filter(histogram.astype(np.float64), 1.5, mode="wrap")
    result: list[np.ndarray] = []
    for index in np.argsort(histogram)[::-1]:
        theta = 0.5 * (edges[index] + edges[index + 1])
        direction = np.array([np.sin(theta), np.cos(theta)], dtype=np.float64)
        # Canonical unoriented sign: prefer image-right; vertical prefers down.
        if direction[1] < 0 or (abs(float(direction[1])) < 1e-12 and direction[0] < 0):
            direction = -direction
        if any(abs(float(np.dot(direction, existing))) > 0.995 for existing in result):
            continue
        result.append(direction)
        if len(result) >= int(max_directions):
            break
    return result


def _infer_grid_shapes(
    points: np.ndarray,
    *,
    xp_guess: float,
    yp_guess: float,
) -> list[tuple[int, int]]:
    """Infer plausible ``(n_cols,n_rows)`` from complete line structure."""
    shapes: list[tuple[float, int, int]] = []
    for direction in _dominant_neighbor_directions(points):
        normal = np.array([-direction[1], direction[0]], dtype=np.float64)
        projection = points @ normal
        for expected_spacing, interpretation in (
            (yp_guess, "rows"),
            (xp_guess, "columns"),
        ):
            grouped = _cluster_lines_auto(projection, expected_spacing)
            if grouped is None:
                continue
            labels, counts, rms_fraction = grouped
            if len(counts) < 2:
                continue
            typical = int(round(float(np.median(counts))))
            if typical < 2 or np.any(counts != typical):
                continue
            n_lines = int(len(counts))
            if n_lines * typical != len(points):
                continue
            if interpretation == "rows":
                n_cols, n_rows = typical, n_lines
            else:
                n_cols, n_rows = n_lines, typical
            # Prefer period/count combinations compatible with image-space extent.
            score = float(rms_fraction)
            if not any(existing[1:] == (n_cols, n_rows) for existing in shapes):
                shapes.append((score, n_cols, n_rows))
    shapes.sort(key=lambda item: item[0])
    return [(cols, rows) for _, cols, rows in shapes[:8]]


def _cluster_lines_auto(
    projection: np.ndarray,
    expected_spacing: float,
) -> tuple[np.ndarray, np.ndarray, float] | None:
    order = np.argsort(projection)
    sorted_values = np.asarray(projection, dtype=np.float64)[order]
    if len(sorted_values) < 4:
        return None
    gaps = np.diff(sorted_values)
    # Within-line spread is sub-pixel to a few pixels; row-to-row gaps are near
    # the supplied period.  The expected spacing is only guidance, not a fit.
    split_threshold = max(0.75, 0.25 * float(expected_spacing))
    split = np.flatnonzero(gaps > split_threshold) + 1
    groups = np.split(order, split)
    groups = [group for group in groups if len(group)]
    if len(groups) < 2:
        return None
    labels = np.empty(len(projection), dtype=np.int64)
    centers = []
    counts = []
    residuals = []
    for label, group in enumerate(groups):
        values = projection[group]
        center = float(np.mean(values))
        labels[group] = label
        centers.append(center)
        counts.append(len(group))
        residuals.extend((values - center).tolist())
    centers = np.asarray(centers)
    spacing = float(np.median(np.diff(centers))) if len(centers) > 1 else 0.0
    if spacing <= 0:
        return None
    rms_fraction = float(np.sqrt(np.mean(np.square(residuals))) / spacing)
    return labels, np.asarray(counts, dtype=np.int64), rms_fraction


def _fit_finite_grid_geometry(
    points: np.ndarray,
    *,
    n_cols: int,
    n_rows: int,
    xp_guess: float,
    yp_guess: float,
    stagger_mode: str,
    stagger_axis: str,
    stagger_fraction: float,
) -> dict[str, Any] | None:
    directions = _dominant_neighbor_directions(points)
    if not directions:
        return None
    best = None

    for line_kind, n_lines, points_per_line in (
        ("rows", n_rows, n_cols),
        ("columns", n_cols, n_rows),
    ):
        for direction in directions:
            row_fit = _fit_lines_for_direction(points, direction, points_per_line, n_lines)
            if row_fit is None:
                continue
            line_score, labels, counts, line_rms = row_fit
            if np.any(counts != points_per_line):
                continue

            for reverse_lines in (False, True):
                for reverse_within in (False, True):
                    ordered = _ordered_finite_correspondence(
                        points,
                        direction=direction,
                        labels=labels,
                        n_lines=n_lines,
                        points_per_line=points_per_line,
                        line_kind=line_kind,
                        reverse_lines=reverse_lines,
                        reverse_within=reverse_within,
                    )
                    if ordered is None:
                        continue
                    observed, logical_indices = ordered
                    model_candidates = _stagger_model_candidates(
                        observed,
                        logical_indices,
                        stagger_mode=stagger_mode,
                        stagger_axis=stagger_axis,
                        stagger_fraction=stagger_fraction,
                    )
                    for axis, fraction, stagger_source, stagger_penalty in model_candidates:
                        fitted = _fit_geometry_model(observed, logical_indices, axis, fraction)
                        if fitted is None:
                            continue
                        origin, basis_x, basis_y, rms = fitted
                        px = float(np.linalg.norm(basis_x))
                        py = float(np.linalg.norm(basis_y))
                        if px <= 1e-9 or py <= 1e-9:
                            continue
                        period_penalty = 2.0 * (
                            ((px - xp_guess) / xp_guess) ** 2
                            + ((py - yp_guess) / yp_guess) ** 2
                        )
                        # Resolve otherwise-equivalent sign/axis choices in a stable
                        # image-space convention. This is only a tie-breaker.
                        orientation_penalty = 0.0
                        if basis_x[1] < 0:
                            orientation_penalty += 0.5
                        if basis_y[0] < 0:
                            orientation_penalty += 0.5
                        score = (
                            float(rms)
                            + float(stagger_penalty)
                            + 0.25 * float(line_score)
                            + period_penalty
                            + orientation_penalty
                        )
                        item = {
                            "score": score,
                            "fit_rms_px": rms,
                            "origin_rc": origin,
                            "basis_x_rc": basis_x,
                            "basis_y_rc": basis_y,
                            "n_cols": n_cols,
                            "n_rows": n_rows,
                            "stagger_axis": axis,
                            "stagger_fraction": fraction,
                            "stagger_source": stagger_source,
                            "line_inference": {
                                "kind": line_kind,
                                "rms_fraction": float(line_rms),
                                "reverse_lines": bool(reverse_lines),
                                "reverse_within": bool(reverse_within),
                            },
                        }
                        if best is None or item["score"] < best["score"]:
                            best = item
    return best


def _fit_lines_for_direction(
    points: np.ndarray,
    direction: np.ndarray,
    points_per_line: int,
    n_lines: int,
) -> tuple[float, np.ndarray, np.ndarray, float] | None:
    normal = np.array([-direction[1], direction[0]], dtype=np.float64)
    projection = points @ normal
    centers, labels = _kmeans_1d(projection, n_lines)
    counts = np.bincount(labels, minlength=n_lines)
    if len(centers) != n_lines or np.any(counts == 0):
        return None
    separation = float(np.median(np.diff(centers))) if n_lines > 1 else 1.0
    if not np.isfinite(separation) or abs(separation) <= 1e-9:
        return None
    residual = projection - centers[labels]
    rms_fraction = float(np.sqrt(np.mean(residual * residual)) / abs(separation))
    count_error = float(
        np.mean(np.abs(counts - int(points_per_line))) / max(int(points_per_line), 1)
    )
    return count_error + 2.0 * rms_fraction, labels, counts, rms_fraction


def _ordered_finite_correspondence(
    points: np.ndarray,
    *,
    direction: np.ndarray,
    labels: np.ndarray,
    n_lines: int,
    points_per_line: int,
    line_kind: str,
    reverse_lines: bool,
    reverse_within: bool,
) -> tuple[np.ndarray, np.ndarray] | None:
    observed = []
    indices = []
    for line_label in range(n_lines):
        ids = np.flatnonzero(labels == line_label)
        if len(ids) != points_per_line:
            return None
        ordered = ids[np.argsort(points[ids] @ direction)]
        if reverse_within:
            ordered = ordered[::-1]
        logical_line = n_lines - 1 - line_label if reverse_lines else line_label
        for rank, detection_id in enumerate(ordered):
            if line_kind == "rows":
                i, j = rank, logical_line
            else:
                i, j = logical_line, rank
            observed.append(points[detection_id])
            indices.append((i, j))
    return np.asarray(observed, dtype=np.float64), np.asarray(indices, dtype=np.int64)


def _stagger_model_candidates(
    observed: np.ndarray,
    logical_indices: np.ndarray,
    *,
    stagger_mode: str,
    stagger_axis: str,
    stagger_fraction: float,
) -> list[tuple[str | None, float, str, float]]:
    if stagger_mode == "none":
        return [(None, 0.0, "none", 0.0)]
    if stagger_mode == "manual":
        return [(stagger_axis, float(stagger_fraction), "manual", 0.0)]

    candidates: list[tuple[str | None, float, str, float]] = [
        (None, 0.0, "auto-none", 0.0)
    ]
    axes = ("rows", "columns") if stagger_axis == "auto" else (stagger_axis,)
    for axis in axes:
        inferred = _infer_stagger(observed, logical_indices, axis)
        if inferred is None:
            continue
        fraction, perpendicular_ratio = inferred
        # Canonicalize an alternating stagger modulo one full period. Fractions
        # above 0.5 have an equivalent opposite-sign representation.
        fraction = float(fraction % 1.0)
        if fraction > 0.5:
            fraction -= 1.0
        # Do not label numerical noise as structural stagger.  The explicit
        # non-staggered model already covers shifts below a few percent of a period.
        if abs(fraction) < 0.03:
            continue
        candidates.append((
            axis,
            fraction,
            "auto",
            3.0 * float(perpendicular_ratio),
        ))
    return candidates


def _infer_stagger(
    observed: np.ndarray,
    logical_indices: np.ndarray,
    axis: str,
) -> tuple[float, float] | None:
    i = logical_indices[:, 0].astype(np.float64)
    j = logical_indices[:, 1].astype(np.float64)
    parity = (j % 2.0) if axis == "rows" else (i % 2.0)
    if not np.any(parity == 0) or not np.any(parity == 1):
        return None
    design = np.column_stack([i, j, parity, np.ones(len(i), dtype=np.float64)])
    coefficients, _, _, _ = np.linalg.lstsq(design, observed, rcond=None)
    basis_x = coefficients[0]
    basis_y = coefficients[1]
    parity_vector = coefficients[2]
    shift_basis = basis_x if axis == "rows" else basis_y
    norm_sq = float(np.dot(shift_basis, shift_basis))
    if norm_sq <= 1e-12:
        return None
    fraction = float(np.dot(parity_vector, shift_basis) / norm_sq)
    perpendicular = parity_vector - fraction * shift_basis
    ratio = float(np.linalg.norm(perpendicular) / max(np.linalg.norm(shift_basis), 1e-12))
    return fraction, ratio


def _fit_geometry_model(
    observed: np.ndarray,
    logical_indices: np.ndarray,
    stagger_axis: str | None,
    stagger_fraction: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float] | None:
    logical = logical_indices.astype(np.float64).copy()
    if stagger_axis == "rows":
        logical[:, 0] += (logical[:, 1] % 2.0) * float(stagger_fraction)
    elif stagger_axis == "columns":
        logical[:, 1] += (logical[:, 0] % 2.0) * float(stagger_fraction)
    design = np.column_stack([logical[:, 0], logical[:, 1], np.ones(len(logical))])
    coefficients, _, rank, _ = np.linalg.lstsq(design, observed, rcond=None)
    if rank < 3:
        return None
    basis_x = coefficients[0]
    basis_y = coefficients[1]
    origin = coefficients[2]
    if abs(float(np.linalg.det(np.column_stack([basis_x, basis_y])))) < 1e-9:
        return None
    predicted = design @ coefficients
    residual = observed - predicted
    rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
    return origin, basis_x, basis_y, rms


def _logical_indices(n_cols: int, n_rows: int) -> np.ndarray:
    return np.asarray(
        [(i, j) for j in range(int(n_rows)) for i in range(int(n_cols))],
        dtype=np.int64,
    )


def _predict_grid_points(
    origin: np.ndarray,
    basis_x: np.ndarray,
    basis_y: np.ndarray,
    logical_indices: np.ndarray,
    stagger_axis: str | None,
    stagger_fraction: float,
) -> np.ndarray:
    logical = np.asarray(logical_indices, dtype=np.float64).copy()
    if stagger_axis == "rows":
        logical[:, 0] += (logical[:, 1] % 2.0) * float(stagger_fraction)
    elif stagger_axis == "columns":
        logical[:, 1] += (logical[:, 0] % 2.0) * float(stagger_fraction)
    return (
        np.asarray(origin, dtype=np.float64)[None, :]
        + logical[:, 0, None] * np.asarray(basis_x, dtype=np.float64)[None, :]
        + logical[:, 1, None] * np.asarray(basis_y, dtype=np.float64)[None, :]
    )


def _kmeans_1d(values: np.ndarray, count: int, max_iterations: int = 30):
    values = np.asarray(values, dtype=np.float64)
    count = int(count)
    if values.size < count or count < 1:
        return np.empty((0,), dtype=np.float64), np.empty(values.shape, dtype=np.int64)
    low, high = np.percentile(values, [0.2, 99.8])
    centers = np.linspace(float(low), float(high), count)
    labels = np.zeros(values.size, dtype=np.int64)
    for _ in range(int(max_iterations)):
        new_labels = np.argmin(
            np.abs(values[:, None] - centers[None, :]), axis=1
        ).astype(np.int64)
        new_centers = np.array(centers, copy=True)
        for index in range(count):
            selected = new_labels == index
            if np.any(selected):
                new_centers[index] = float(np.mean(values[selected]))
        order = np.argsort(new_centers)
        inverse = np.empty(count, dtype=np.int64)
        inverse[order] = np.arange(count, dtype=np.int64)
        new_labels = inverse[new_labels]
        new_centers = new_centers[order]
        if np.array_equal(new_labels, labels) and np.allclose(
            new_centers, centers, rtol=0.0, atol=1e-9
        ):
            labels, centers = new_labels, new_centers
            break
        labels, centers = new_labels, new_centers
    return centers, labels


# ---------------------------------------------------------------------------
# Historical axis-aligned localizer (kept for ULensesController compatibility)
# ---------------------------------------------------------------------------


def _find_best_peak_index(peaks: Tuple) -> int:
    """Return the index of the most suitable detected FFT peak."""
    prominences = peaks[1]["prominences"]
    heights = peaks[1]["peak_heights"]

    if len(prominences) < 2:
        return 0

    sorted_prom_indices = np.argsort(prominences)
    p1_index = sorted_prom_indices[-1]
    p2_index = sorted_prom_indices[-2]

    p1_prom = prominences[p1_index]
    p2_prom = prominences[p2_index]
    p1_height = heights[p1_index]
    p2_height = heights[p2_index]

    diff_prom = np.abs((p1_prom - p2_prom) / (p1_prom + p2_prom))
    diff_height = np.abs((p1_height - p2_height) / (p1_height + p2_height))

    tol = 0.2
    if (diff_prom < tol) and (diff_height < tol) and (p1_height < p2_height):
        return p2_index

    return p1_index


def _estimate_period(period_guess: float, input_data: np.ndarray) -> float:
    """Estimate the dominant period in a 1D signal using FFT + Gaussian fit."""
    data_size = input_data.size

    fft_max_index = data_size // 2 + 1
    abs_fft = np.abs(np.fft.fft(input_data)[0:fft_max_index])
    fft_freqs = np.fft.fftfreq(fft_max_index)

    target_freq = 2 / period_guess
    tol = 0.05
    abs_fft[fft_freqs < target_freq - tol] = 0

    peaks = find_peaks(abs_fft, prominence=[0, np.inf], width=0, height=0)
    best_peak_index = _find_best_peak_index(peaks)
    peak_index = peaks[0][best_peak_index]
    peak_width = peaks[1]["widths"][best_peak_index]

    peak_spread = 6 * peak_width
    win_min = int(max(0, peak_index - peak_spread))
    win_max = int(min(peak_index + peak_spread, fft_max_index))
    win_range = np.arange(start=win_min, step=1, stop=win_max, dtype=int)

    def _gauss_fun(x0, x, y):
        a, b, mu, sigma = x0
        return (a + b * np.exp(-(x - mu) ** 2 / (2 * sigma ** 2))) - y

    x0_guesses = np.array([
        np.min(abs_fft),
        np.max(abs_fft) - np.min(abs_fft),
        peak_index,
        peak_width,
    ])
    res_gauss_fit = least_squares(
        _gauss_fun,
        x0_guesses,
        args=(win_range, abs_fft[win_range]),
        ftol=1e-12,
        xtol=1e-12,
    )
    return np.abs(data_size / res_gauss_fit.x[2])


def _estimate_offset(est_period: float, input_data: np.ndarray) -> float:
    """Estimate the phase offset in a 1D periodic signal."""

    def _cos_fun(x0, x, y):
        offset = x0
        return np.cos(np.pi * (x - offset) / est_period) ** 6 - y

    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(
        _cos_fun,
        x0=0.0,
        args=(x_range, input_data),
        ftol=1e-12,
        xtol=1e-12,
    )
    return np.mod(res_cos_fit.x[0], est_period)


def _optimize_parameters(
    est_period: float,
    est_offset: float,
    input_data: np.ndarray,
) -> Tuple[float, float]:
    """Jointly refine period and offset by least-squares fitting."""

    def _cos_fun(x0, x, y):
        period, offset = x0
        return np.cos(np.pi * (x - offset) / period) ** 6 - y

    x0_guesses = np.array([est_period, est_offset])
    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(
        fun=_cos_fun,
        x0=x0_guesses,
        args=(x_range, input_data),
        ftol=1e-12,
        xtol=1e-12,
    )

    opt_period = np.abs(res_cos_fit.x[0])
    opt_offset = np.mod(res_cos_fit.x[1], opt_period)
    return opt_period, opt_offset


def localizer(
    img_data: np.ndarray,
    xp_guess: float = 10.0,
    yp_guess: float = 10.0,
) -> LocalizationResult:
    """Find axis-aligned grid periods/offsets (legacy compatibility API)."""
    if img_data.ndim == 3:
        _, num_rows, num_cols = img_data.shape
        img_stack_sum = np.double(img_data.sum(axis=0))
    elif img_data.ndim == 2:
        num_rows, num_cols = img_data.shape
        img_stack_sum = np.double(img_data)
    else:
        raise ValueError(f"Expected 2D or 3D array, got {img_data.ndim}D")

    sigma_low = 2.0
    sigma_high = (xp_guess + yp_guess) / 2
    img_stack_sum -= gaussian_filter(img_stack_sum, sigma_high)
    img_stack_sum = gaussian_filter(img_stack_sum, sigma_low)
    img_stack_sum -= img_stack_sum.mean()

    x_img_avg = img_stack_sum.mean(axis=0)
    y_img_avg = img_stack_sum.mean(axis=1)

    xp_est = _estimate_period(xp_guess, x_img_avg)
    xo_est = _estimate_offset(xp_est, x_img_avg)
    yp_est = _estimate_period(yp_guess, y_img_avg)
    yo_est = _estimate_offset(yp_est, y_img_avg)

    xp, xo = _optimize_parameters(xp_est, xo_est, x_img_avg)
    yp, yo = _optimize_parameters(yp_est, yo_est, y_img_avg)

    nx_c = int(np.ceil((num_cols - xo) / xp))
    ny_c = int(np.ceil((num_rows - yo) / yp))

    return LocalizationResult(
        xp,
        xo,
        yp,
        yo,
        nx_c,
        ny_c,
        num_cols,
        num_rows,
    )

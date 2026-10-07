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

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks
from scipy.spatial import cKDTree

__all__ = [
    "LocalizationResult",
    "GridLocalizationResult",
    "GridLocalizationError",
    "localizer",
    "localize_grid",
    "refine_grid_foci",
    "search_missing_grid_foci",
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


class GridLocalizationError(RuntimeError):
    """Grid-localization failure carrying the partial detection evidence."""

    def __init__(
        self,
        message: str,
        *,
        detected_points_rc: np.ndarray | None = None,
        crop_coord: tuple[int, int, int, int] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.detected_points_rc = (
            None if detected_points_rc is None
            else np.asarray(detected_points_rc, dtype=np.float64)
        )
        self.crop_coord = crop_coord
        self.diagnostics = dict(diagnostics or {})



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
    matched_mask: np.ndarray
    measured_points_rc: np.ndarray
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
        matched = np.asarray(self.matched_mask, dtype=bool)
        measured = np.asarray(self.measured_points_rc, dtype=np.float64)
        if points.shape != (int(self.n_rows) * int(self.n_cols), 2):
            raise ValueError(
                "grid_points_rc must contain exactly n_rows * n_cols points"
            )
        if indices.shape != points.shape:
            raise ValueError("logical_indices must match grid_points_rc shape")
        if detections.ndim != 2 or detections.shape[1] != 2:
            raise ValueError("detected_points_rc must have shape (N, 2)")
        if matched.shape != (len(points),):
            raise ValueError("matched_mask must have shape (n_rows * n_cols,)")
        if measured.shape != points.shape:
            raise ValueError("measured_points_rc must match grid_points_rc shape")
        if np.any(matched) and not np.all(np.isfinite(measured[matched])):
            raise ValueError("matched measured_points_rc entries must be finite")
        if self.stagger_axis not in (None, "rows", "columns"):
            raise ValueError("stagger_axis must be None, 'rows', or 'columns'")
        object.__setattr__(self, "origin_rc", origin)
        object.__setattr__(self, "basis_x_rc", basis_x)
        object.__setattr__(self, "basis_y_rc", basis_y)
        object.__setattr__(self, "grid_points_rc", points)
        object.__setattr__(self, "logical_indices", indices)
        object.__setattr__(self, "detected_points_rc", detections)
        object.__setattr__(self, "matched_mask", matched)
        object.__setattr__(self, "measured_points_rc", measured)

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

    @property
    def matched_count(self) -> int:
        return int(np.count_nonzero(self.matched_mask))

    @property
    def missing_count(self) -> int:
        return int(len(self.matched_mask) - self.matched_count)

    @property
    def missing_points_rc(self) -> np.ndarray:
        return np.asarray(self.grid_points_rc[~self.matched_mask], dtype=np.float64)


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
    crop_threshold: float = 0.4,
    crop_margin_px: int = 1,
    threshold_rel: float = 0.10,
    gaussian_sigma: float = 1.0,
    min_distance_fraction: float = 0.35,
    stagger_mode: str = "auto",
    stagger_axis: str = "auto",
    stagger_fraction: float = 0.5,
) -> GridLocalizationResult:
    """Estimate finite tilted-grid geometry from a 2-D image or 3-D stack.

    ``n_cols``/``n_rows`` describe the expected finite topology, not an exact
    detector-count contract. Missing sites and extra candidate detections are
    tolerated as long as enough of the lattice can be registered robustly.

    Detection follows the proven SLMcore-style workflow: automatically crop the
    bright region, subtract a broad local background, find separated maxima and
    refine their centers.  The crop and candidate points are retained as runtime
    diagnostics so callers can inspect failed attempts.
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
    crop_threshold = float(crop_threshold)
    if not 0.0 <= crop_threshold <= 1.0:
        raise ValueError("crop_threshold must be between 0 and 1")
    crop_margin_px = int(crop_margin_px)
    if crop_margin_px < 0:
        raise ValueError("crop_margin_px must be non-negative")
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
    detected, crop_coord, detection_diagnostics = _detect_grid_spots(
        image,
        xp_guess=xp_guess,
        yp_guess=yp_guess,
        crop_threshold=crop_threshold,
        crop_margin_px=crop_margin_px,
        threshold_rel=threshold_rel,
        gaussian_sigma=gaussian_sigma,
        min_distance_fraction=min_distance_fraction,
        expected_n=expected_n,
    )
    failure_context = {
        **detection_diagnostics,
        "crop_coord": tuple(int(v) for v in crop_coord),
        "detected_count": int(len(detected)),
        "period_guess_px": (xp_guess, yp_guess),
        "expected_count": expected_n,
    }
    if len(detected) < 4:
        raise GridLocalizationError(
            "At least four grid spots are required",
            detected_points_rc=detected,
            crop_coord=crop_coord,
            diagnostics=failure_context,
        )

    if n_cols is None:
        shapes = _infer_grid_shapes(
            detected,
            xp_guess=xp_guess,
            yp_guess=yp_guess,
        )
        if not shapes:
            raise GridLocalizationError(
                "Auto grid-size inference could not recover a finite grid. "
                "Enter n_cols and n_rows manually or adjust detection parameters.",
                detected_points_rc=detected,
                crop_coord=crop_coord,
                diagnostics=failure_context,
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
            message = (
                "Could not fit the finite grid topology. Try manual n_cols/n_rows "
                "or adjust grid detection parameters."
            )
        else:
            message = (
                "Could not fit the manual finite topology. Check n_cols/n_rows "
                "and grid detection parameters."
            )
        raise GridLocalizationError(
            message,
            detected_points_rc=detected,
            crop_coord=crop_coord,
            diagnostics={
                **failure_context,
                "grid_size_source": shape_source,
                "candidate_shapes": [tuple(map(int, shape)) for shape in shapes],
            },
        )

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
        **failure_context,
        "grid_size_source": shape_source,
        "candidate_shapes": [tuple(map(int, shape)) for shape in shapes],
        "fit_rms_px": float(best["fit_rms_px"]),
        "period_fit_px": (
            float(np.linalg.norm(best["basis_x_rc"])),
            float(np.linalg.norm(best["basis_y_rc"])),
        ),
        "stagger_source": best["stagger_source"],
        "line_inference": best.get("line_inference", {}),
        "matched_count": int(np.count_nonzero(best["matched_mask"])),
        "missing_count": int(np.count_nonzero(~best["matched_mask"])),
        "unmatched_detection_count": int(best.get("unmatched_detection_count", 0)),
        "matching_gate_px": float(best.get("matching_gate_px", np.nan)),
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
        matched_mask=best["matched_mask"],
        measured_points_rc=best["measured_points_rc"],
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
    matched_only: bool = False,
) -> np.ndarray:
    """Refine predicted grid sites independently to focus centers.

    ``matched_only`` is used by transform calibration: only sites supported by a
    coarse detected spot are refined, so missing sites are never silently turned
    into synthetic transform correspondences.  uLenses may still display the
    complete predicted lattice directly from ``grid_points_rc``.
    """
    image = _as_grid_image(img_data)
    radius = int(search_radius_px)
    if radius < 0:
        raise ValueError("search_radius_px must be non-negative")
    sigma = float(gaussian_sigma)
    if sigma < 0:
        raise ValueError("gaussian_sigma must be non-negative")
    search = gaussian_filter(image.astype(np.float64, copy=False), sigma) if sigma else image

    sites = np.asarray(grid.grid_points_rc)
    if matched_only:
        sites = sites[np.asarray(grid.matched_mask, dtype=bool)]
    return _refine_focus_sites(image, search, sites, radius)


def _refine_focus_sites(
    image: np.ndarray, search: np.ndarray, sites: np.ndarray, radius: int
) -> np.ndarray:
    refined = []
    h, w = image.shape
    for predicted_row, predicted_col in np.asarray(sites, dtype=np.float64):
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


def search_missing_grid_foci(
    img_data: np.ndarray,
    grid: GridLocalizationResult,
    *,
    search_radius_px: int = 5,
    gaussian_sigma: float = 1.0,
    threshold_rel: float = 0.05,
) -> GridLocalizationResult:
    """Try to recover missing finite-grid sites using the fitted grid as guidance.

    This is intentionally explicit rather than part of normal transform
    localization. Only local peaks with real processed-image evidence above
    ``threshold_rel`` are accepted.
    """
    if grid.missing_count == 0:
        return grid
    image = _as_grid_image(img_data)
    radius = max(1, int(search_radius_px))
    sigma = max(0.0, float(gaussian_sigma))
    threshold = float(threshold_rel)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold_rel must be between 0 and 1")

    crop_coord = tuple(
        int(v) for v in grid.diagnostics.get(
            "crop_coord", (0, image.shape[0], 0, image.shape[1])
        )
    )
    y0, y1, x0, x1 = crop_coord
    cropped = image[y0:y1, x0:x1]
    processed = _preprocess_grid_signal(cropped, sigma)
    matched = np.asarray(grid.matched_mask, dtype=bool).copy()
    measured = np.asarray(grid.measured_points_rc, dtype=np.float64).copy()
    recovered = 0

    for index in np.flatnonzero(~matched):
        predicted_row, predicted_col = grid.grid_points_rc[index]
        local_row = float(predicted_row) - y0
        local_col = float(predicted_col) - x0
        center_row = int(round(local_row))
        center_col = int(round(local_col))
        r0 = max(0, center_row - radius)
        r1 = min(processed.shape[0], center_row + radius + 1)
        c0 = max(0, center_col - radius)
        c1 = min(processed.shape[1], center_col + radius + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        patch = processed[r0:r1, c0:c1]
        flat = int(np.argmax(patch))
        peak_value = float(patch.flat[flat])
        if peak_value < threshold:
            continue
        peak_r, peak_c = np.unravel_index(flat, patch.shape)
        global_site = np.asarray([[
            float(y0 + r0 + peak_r), float(x0 + c0 + peak_c)
        ]])
        search_image = gaussian_filter(image, sigma) if sigma else image
        refined = _refine_focus_sites(image, search_image, global_site, radius)[0]
        measured[index] = refined
        matched[index] = True
        recovered += 1

    if recovered == 0:
        return grid

    fitted = _fit_geometry_model(
        measured[matched], grid.logical_indices[matched],
        grid.stagger_axis, grid.stagger_fraction,
    )
    if fitted is None:
        return grid
    origin, basis_x, basis_y, rms = fitted
    predicted = _predict_grid_points(
        origin, basis_x, basis_y, grid.logical_indices,
        grid.stagger_axis, grid.stagger_fraction,
    )
    diagnostics = dict(grid.diagnostics)
    diagnostics.update({
        "fit_rms_px": float(rms),
        "matched_count": int(np.count_nonzero(matched)),
        "missing_count": int(np.count_nonzero(~matched)),
        "recovered_missing_count": int(
            diagnostics.get("recovered_missing_count", 0)
        ) + int(recovered),
    })
    return replace(
        grid,
        origin_rc=origin,
        basis_x_rc=basis_x,
        basis_y_rc=basis_y,
        grid_points_rc=predicted,
        matched_mask=matched,
        measured_points_rc=measured,
        diagnostics=diagnostics,
    )

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
    crop_threshold: float,
    crop_margin_px: int,
    threshold_rel: float,
    gaussian_sigma: float,
    min_distance_fraction: float,
    expected_n: int | None,
) -> tuple[np.ndarray, tuple[int, int, int, int], dict[str, Any]]:
    """SLMcore-style crop, local-background suppression and peak detection."""
    cropped, crop_coord = _crop_grid_image(
        image, threshold=float(crop_threshold), margin=int(crop_margin_px)
    )
    processed = _preprocess_grid_signal(cropped, float(gaussian_sigma))
    min_distance = max(
        1.0, min(float(xp_guess), float(yp_guess)) * float(min_distance_fraction)
    )
    peaks, scores = _grid_local_maxima(
        processed, threshold_rel=float(threshold_rel), min_distance=min_distance
    )
    if len(peaks) == 0:
        raise GridLocalizationError(
            "No grid spots were detected",
            detected_points_rc=np.empty((0, 2), dtype=np.float64),
            crop_coord=crop_coord,
            diagnostics={
                "crop_coord": crop_coord,
                "crop_shape": tuple(int(v) for v in cropped.shape),
                "threshold_rel": float(threshold_rel),
                "crop_threshold": float(crop_threshold),
                "min_distance_px": float(min_distance),
            },
        )

    # Keep all plausible detections.  A manual finite NxM is a topology prior,
    # not a request to truncate the detector to exactly NxM brightest peaks.
    # Bound pathological noise only very loosely for predictable runtime.
    if expected_n is not None and len(peaks) > max(4 * int(expected_n), int(expected_n) + 128):
        keep = np.argsort(scores)[::-1][: max(4 * int(expected_n), int(expected_n) + 128)]
        peaks = peaks[keep]

    refined_local = _refine_grid_detection_centers(cropped, peaks, min_distance)
    y0, _y1, x0, _x1 = crop_coord
    refined = np.asarray(refined_local, dtype=np.float64)
    refined[:, 0] += float(y0)
    refined[:, 1] += float(x0)
    return refined, crop_coord, {
        "crop_shape": tuple(int(v) for v in cropped.shape),
        "crop_threshold": float(crop_threshold),
        "crop_margin_px": int(crop_margin_px),
        "threshold_rel": float(threshold_rel),
        "gaussian_sigma": float(gaussian_sigma),
        "min_distance_px": float(min_distance),
    }


def _crop_grid_image(
    image: np.ndarray, *, threshold: float, margin: int
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    source = np.asarray(image, dtype=np.float64)
    height, width = source.shape
    maximum = float(np.max(source)) if source.size else 0.0
    if maximum <= 0:
        coord = (0, height, 0, width)
    else:
        mask = source > maximum * float(threshold)
        yy, xx = np.nonzero(mask)
        if yy.size == 0:
            coord = (0, height, 0, width)
        else:
            coord = (
                max(0, int(yy.min()) - int(margin)),
                min(height, int(yy.max()) + 1 + int(margin)),
                max(0, int(xx.min()) - int(margin)),
                min(width, int(xx.max()) + 1 + int(margin)),
            )
    y0, y1, x0, x1 = coord
    return np.array(source[y0:y1, x0:x1], dtype=np.float64, copy=True), coord


def _preprocess_grid_signal(image: np.ndarray, blur_sigma: float) -> np.ndarray:
    signal = np.asarray(image, dtype=np.float64)
    if signal.size == 0:
        return np.array(signal, copy=True)
    if blur_sigma > 0:
        smoothed = gaussian_filter(signal, blur_sigma)
        background = gaussian_filter(signal, max(3.0, 4.0 * blur_sigma))
        signal = smoothed - background
    else:
        signal = signal - float(np.median(signal))
    signal = np.maximum(signal, 0.0)
    maximum = float(np.max(signal))
    if maximum > 0:
        signal = signal / maximum
    return signal


def _grid_local_maxima(
    processed: np.ndarray, *, threshold_rel: float, min_distance: float
) -> tuple[np.ndarray, np.ndarray]:
    """Separated local maxima with the same greedy semantics as SLMcore."""
    radius = max(1, int(round(float(min_distance))))
    maximum = maximum_filter(processed, size=2 * radius + 1, mode="nearest")
    mask = (processed == maximum) & (processed >= float(threshold_rel))
    yy, xx = np.nonzero(mask)
    if yy.size == 0:
        return np.empty((0, 2), dtype=np.float64), np.empty((0,), dtype=np.float64)
    values = processed[yy, xx]
    order = np.argsort(values)[::-1]
    min_distance_sq = float(min_distance) ** 2
    cell_size = max(float(min_distance), 1.0)
    selected: list[tuple[float, float]] = []
    selected_scores: list[float] = []
    cells: dict[tuple[int, int], list[int]] = {}
    for raw_index in order:
        row = float(yy[raw_index])
        col = float(xx[raw_index])
        cell_col = int(np.floor(col / cell_size))
        cell_row = int(np.floor(row / cell_size))
        keep = True
        for neighbor_row in range(cell_row - 1, cell_row + 2):
            if not keep:
                break
            for neighbor_col in range(cell_col - 1, cell_col + 2):
                for selected_index in cells.get((neighbor_col, neighbor_row), ()):
                    selected_row, selected_col = selected[selected_index]
                    dr = row - selected_row
                    dc = col - selected_col
                    if dr * dr + dc * dc < min_distance_sq:
                        keep = False
                        break
                if not keep:
                    break
        if not keep:
            continue
        selected_index = len(selected)
        selected.append((row, col))
        selected_scores.append(float(values[raw_index]))
        cells.setdefault((cell_col, cell_row), []).append(selected_index)
    return np.asarray(selected, dtype=np.float64), np.asarray(selected_scores, dtype=np.float64)


def _refine_grid_detection_centers(
    cropped: np.ndarray, peaks: np.ndarray, min_distance: float
) -> np.ndarray:
    radius = max(1, min(3, int(round(float(min_distance))) // 2))
    refined = []
    for row_value, col_value in np.asarray(peaks, dtype=np.float64):
        row = int(round(float(row_value)))
        col = int(round(float(col_value)))
        r0, r1 = max(0, row - radius), min(cropped.shape[0], row + radius + 1)
        c0, c1 = max(0, col - radius), min(cropped.shape[1], col + radius + 1)
        patch = np.asarray(cropped[r0:r1, c0:c1], dtype=np.float64)
        weights = np.clip(patch - float(np.min(patch)), 0.0, None)
        total = float(np.sum(weights))
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
    """Infer plausible finite shapes while tolerating sparse missing/extra sites."""
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
            _labels, counts, rms_fraction = grouped
            if len(counts) < 2:
                continue
            typical = int(round(float(np.median(counts))))
            if typical < 2:
                continue
            deviations = np.abs(counts - typical)
            close_fraction = float(np.mean(deviations <= max(1, int(np.ceil(0.08 * typical)))))
            if close_fraction < 0.65:
                continue
            n_lines = int(len(counts))
            capacity = max(1, n_lines * typical)
            count_error = float(np.sum(deviations) / capacity)
            if count_error > 0.20:
                continue
            if interpretation == "rows":
                n_cols, n_rows = typical, n_lines
            else:
                n_cols, n_rows = n_lines, typical
            score = float(rms_fraction) + 2.0 * count_error
            existing = next(
                (index for index, item in enumerate(shapes) if item[1:] == (n_cols, n_rows)),
                None,
            )
            item = (score, n_cols, n_rows)
            if existing is None:
                shapes.append(item)
            elif score < shapes[existing][0]:
                shapes[existing] = item
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
    """Fit expected finite topology without requiring a perfect detector count."""
    directions = _dominant_neighbor_directions(points)
    if not directions:
        return None
    logical_full = _logical_indices(n_cols, n_rows)
    best = None

    for line_kind, n_lines, points_per_line in (
        ("rows", n_rows, n_cols),
        ("columns", n_cols, n_rows),
    ):
        for direction in directions:
            line_fit = _fit_lines_for_direction(points, direction, points_per_line, n_lines)
            if line_fit is None:
                continue
            line_score, labels, counts, line_rms = line_fit
            complete_labels = [
                label for label in range(n_lines)
                if int(counts[label]) == int(points_per_line)
            ]
            minimum_complete = min(n_lines, max(2, int(np.ceil(0.20 * n_lines))))
            if len(complete_labels) < minimum_complete:
                continue

            for reverse_lines in (False, True):
                for reverse_within in (False, True):
                    ordered = _ordered_finite_correspondence(
                        points,
                        direction=direction,
                        labels=labels,
                        complete_labels=complete_labels,
                        n_lines=n_lines,
                        points_per_line=points_per_line,
                        line_kind=line_kind,
                        reverse_lines=reverse_lines,
                        reverse_within=reverse_within,
                    )
                    if ordered is None:
                        continue
                    observed, logical_indices = ordered
                    if len(observed) < 6:
                        continue
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
                        origin, basis_x, basis_y, _initial_rms = fitted
                        refined = _refine_grid_registration(
                            points, logical_full,
                            origin=origin, basis_x=basis_x, basis_y=basis_y,
                            stagger_axis=axis, stagger_fraction=fraction,
                        )
                        if refined is None:
                            continue
                        origin = refined["origin_rc"]
                        basis_x = refined["basis_x_rc"]
                        basis_y = refined["basis_y_rc"]
                        rms = float(refined["fit_rms_px"])
                        px = float(np.linalg.norm(basis_x))
                        py = float(np.linalg.norm(basis_y))
                        if px <= 1e-9 or py <= 1e-9:
                            continue
                        period_penalty = 2.0 * (
                            ((px - xp_guess) / xp_guess) ** 2
                            + ((py - yp_guess) / yp_guess) ** 2
                        )
                        orientation_penalty = 0.0
                        if basis_x[1] < 0:
                            orientation_penalty += 0.5
                        if basis_y[0] < 0:
                            orientation_penalty += 0.5
                        missing_fraction = 1.0 - (
                            float(np.count_nonzero(refined["matched_mask"]))
                            / float(len(logical_full))
                        )
                        score = (
                            20.0 * missing_fraction
                            + rms
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
                            "matched_mask": refined["matched_mask"],
                            "measured_points_rc": refined["measured_points_rc"],
                            "unmatched_detection_count": refined["unmatched_detection_count"],
                            "matching_gate_px": refined["matching_gate_px"],
                            "line_inference": {
                                "kind": line_kind,
                                "rms_fraction": float(line_rms),
                                "complete_lines": int(len(complete_labels)),
                                "line_count": int(n_lines),
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
    complete_labels: list[int],
    n_lines: int,
    points_per_line: int,
    line_kind: str,
    reverse_lines: bool,
    reverse_within: bool,
) -> tuple[np.ndarray, np.ndarray] | None:
    observed = []
    indices = []
    for line_label in complete_labels:
        ids = np.flatnonzero(labels == line_label)
        if len(ids) != points_per_line:
            continue
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
    if not observed:
        return None
    return np.asarray(observed, dtype=np.float64), np.asarray(indices, dtype=np.int64)


def _refine_grid_registration(
    points: np.ndarray,
    logical_indices: np.ndarray,
    *,
    origin: np.ndarray,
    basis_x: np.ndarray,
    basis_y: np.ndarray,
    stagger_axis: str | None,
    stagger_fraction: float,
    max_iterations: int = 8,
) -> dict[str, Any] | None:
    points = np.asarray(points, dtype=np.float64)
    origin = np.asarray(origin, dtype=np.float64)
    basis_x = np.asarray(basis_x, dtype=np.float64)
    basis_y = np.asarray(basis_y, dtype=np.float64)
    tree = cKDTree(points)
    minimum_matches = max(6, int(np.ceil(0.60 * len(logical_indices))))
    last_pairs = None

    for _ in range(int(max_iterations)):
        predicted = _predict_grid_points(
            origin, basis_x, basis_y, logical_indices, stagger_axis, stagger_fraction
        )
        gate = max(1.5, 0.45 * min(np.linalg.norm(basis_x), np.linalg.norm(basis_y)))
        target_indices, detection_indices, _distances = _local_one_to_one_grid_matches(
            predicted, points, gate, tree=tree
        )
        if len(target_indices) < minimum_matches:
            return None
        fitted = _fit_geometry_model(
            points[detection_indices], logical_indices[target_indices],
            stagger_axis, stagger_fraction,
        )
        if fitted is None:
            return None
        new_origin, new_basis_x, new_basis_y, _ = fitted
        pairs = (tuple(target_indices.tolist()), tuple(detection_indices.tolist()))
        converged = (
            pairs == last_pairs
            and np.allclose(new_origin, origin, rtol=0.0, atol=1e-4)
            and np.allclose(new_basis_x, basis_x, rtol=0.0, atol=1e-4)
            and np.allclose(new_basis_y, basis_y, rtol=0.0, atol=1e-4)
        )
        origin, basis_x, basis_y = new_origin, new_basis_x, new_basis_y
        last_pairs = pairs
        if converged:
            break

    predicted = _predict_grid_points(
        origin, basis_x, basis_y, logical_indices, stagger_axis, stagger_fraction
    )
    gate = max(1.5, 0.45 * min(np.linalg.norm(basis_x), np.linalg.norm(basis_y)))
    target_indices, detection_indices, distances = _local_one_to_one_grid_matches(
        predicted, points, gate, tree=tree
    )
    if len(target_indices) < minimum_matches:
        return None
    matched = np.zeros(len(logical_indices), dtype=bool)
    measured = np.full((len(logical_indices), 2), np.nan, dtype=np.float64)
    matched[target_indices] = True
    measured[target_indices] = points[detection_indices]
    rms = float(np.sqrt(np.mean(np.square(distances)))) if len(distances) else float("inf")
    return {
        "origin_rc": origin,
        "basis_x_rc": basis_x,
        "basis_y_rc": basis_y,
        "matched_mask": matched,
        "measured_points_rc": measured,
        "fit_rms_px": rms,
        "matching_gate_px": float(gate),
        "unmatched_detection_count": int(len(points) - len(set(detection_indices.tolist()))),
    }


def _local_one_to_one_grid_matches(
    predicted: np.ndarray,
    points: np.ndarray,
    gate: float,
    *,
    tree: cKDTree | None = None,
    k_neighbors: int = 4,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if tree is None:
        tree = cKDTree(points)
    k = max(1, min(int(k_neighbors), len(points)))
    distances, nearest = tree.query(
        np.asarray(predicted, dtype=np.float64),
        k=k, distance_upper_bound=float(gate),
    )
    if k == 1:
        distances = distances[:, None]
        nearest = nearest[:, None]
    edges = []
    for target_index in range(len(predicted)):
        for rank in range(k):
            distance = float(distances[target_index, rank])
            detection_index = int(nearest[target_index, rank])
            if np.isfinite(distance) and detection_index < len(points):
                edges.append((distance, target_index, detection_index))
    edges.sort(key=lambda item: item[0])
    used_targets: set[int] = set()
    used_detections: set[int] = set()
    accepted = []
    for distance, target_index, detection_index in edges:
        if target_index in used_targets or detection_index in used_detections:
            continue
        used_targets.add(target_index)
        used_detections.add(detection_index)
        accepted.append((target_index, detection_index, distance))
    accepted.sort(key=lambda item: item[0])
    return (
        np.asarray([item[0] for item in accepted], dtype=np.int64),
        np.asarray([item[1] for item in accepted], dtype=np.int64),
        np.asarray([item[2] for item in accepted], dtype=np.float64),
    )

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

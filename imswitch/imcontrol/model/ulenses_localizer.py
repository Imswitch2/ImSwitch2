"""uLenses localization adapters and legacy compatibility helpers.

The grid-localization API is intentionally thin: its numerical implementation
lives in :mod:`imswitch.imcommon.algorithms.grid_localization`, which mirrors the
proven SLMcore localization core.  Both the uLenses overlay and transform
calibration therefore share the same crop, spot detection, finite-topology,
stagger inference, affine refinement and FFT-fallback behavior.

The historical axis-aligned :func:`localizer` remains available during the
validation period.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks

from imswitch.imcommon.algorithms.grid_localization import (
    LatticeRegistrationOptions,
    SpotDetectionOptions,
    detect_spots,
    infer_lattice_shape,
    make_lattice_model,
    rectangular_lattice_indices,
    register_lattice,
)
from imswitch.imcommon.algorithms.grid_localization.detection import _preprocess

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
    """Grid-localization failure carrying partial detection evidence."""

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
    """Finite 2-D lattice geometry in full-image ``(row, col)`` coordinates."""

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
            raise ValueError("grid_points_rc must contain exactly n_rows * n_cols points")
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


def _backend_options(
    *,
    xp_guess: float,
    yp_guess: float,
    use_period_prior: bool,
    crop_threshold: float,
    crop_margin_px: int,
    threshold_rel: float,
    gaussian_sigma: float,
    min_distance_fraction: float,
    focus_localization_method: str,
    focus_search_window_px: int,
    registration_mode: str,
    period_tolerance_fraction: float,
    fft_exclude_fraction: float,
    fft_peak_count: int,
    lattice_candidate_search_mode: str,
    matching_gate_fraction: float,
    robust_outlier_sigma: float,
    max_registration_iterations: int,
    min_match_fraction: float,
):
    detection = SpotDetectionOptions(
        crop_threshold=float(crop_threshold),
        crop_margin_px=int(crop_margin_px),
        blur_sigma=float(gaussian_sigma),
        threshold_rel=float(threshold_rel),
        min_distance_fraction=float(min_distance_fraction),
        refinement_method=str(focus_localization_method),
        refinement_window_px=int(focus_search_window_px),
    )
    registration = LatticeRegistrationOptions(
        expected_period_px=(float(xp_guess), float(yp_guess)) if use_period_prior else None,
        period_tolerance_fraction=float(period_tolerance_fraction),
        registration_mode=str(registration_mode),
        fft_exclude_fraction=float(fft_exclude_fraction),
        fft_peak_count=int(fft_peak_count),
        lattice_candidate_search_mode=str(lattice_candidate_search_mode),
        matching_gate_fraction=float(matching_gate_fraction),
        robust_outlier_sigma=float(robust_outlier_sigma),
        max_iterations=int(max_registration_iterations),
        min_match_fraction=float(min_match_fraction),
    )
    return detection, registration


def _full_xy(points_xy: np.ndarray, crop_coord: tuple[int, int, int, int]) -> np.ndarray:
    points = np.asarray(points_xy, dtype=np.float64).copy()
    if points.size == 0:
        return points.reshape(2, -1)
    y0, _y1, x0, _x1 = crop_coord
    points[0] += float(x0)
    points[1] += float(y0)
    return points


def _grid_from_registration(
    image_shape: tuple[int, int],
    registration,
    *,
    transposed: bool = False,
    grid_size_source: str = "",
) -> GridLocalizationResult:
    crop = tuple(int(v) for v in registration.detections.crop_coord)
    expected_xy = _full_xy(registration.expected_positions_px, crop)
    detected_xy = _full_xy(registration.detections.positions_px, crop)
    measured_xy = _full_xy(registration.measured_positions_px, crop)
    indices_xy = np.asarray(registration.model.lattice_indices, dtype=np.int64).T
    linear = np.asarray(registration.affine_linear, dtype=np.float64)

    if not transposed:
        expected_rc = expected_xy[[1, 0], :].T
        detected_rc = detected_xy[[1, 0], :].T
        measured_rc = measured_xy[[1, 0], :].T
        logical = indices_xy
        basis_x_rc = np.array([linear[1, 0], linear[0, 0]], dtype=np.float64)
        basis_y_rc = np.array([linear[1, 1], linear[0, 1]], dtype=np.float64)
        crop_full = crop
        n_cols = int(np.unique(logical[:, 0]).size)
        n_rows = int(np.unique(logical[:, 1]).size)
        stagger_value = float(registration.model.diagnostics.get("stagger", 0.0) or 0.0)
        if abs(stagger_value) < 0.05:
            stagger_value = 0.0
            stagger_axis = None
        else:
            stagger_axis = "rows"
    else:
        # Backend image was transposed: x' is original row and y' original col.
        expected_rc = expected_xy.T
        detected_rc = detected_xy.T
        measured_rc = measured_xy.T
        logical = indices_xy[:, [1, 0]]
        basis_x_rc = np.array([linear[0, 1], linear[1, 1]], dtype=np.float64)
        basis_y_rc = np.array([linear[0, 0], linear[1, 0]], dtype=np.float64)
        y0, y1, x0, x1 = crop
        crop_full = (x0, x1, y0, y1)
        n_cols = int(np.unique(logical[:, 0]).size)
        n_rows = int(np.unique(logical[:, 1]).size)
        stagger_value = float(registration.model.diagnostics.get("stagger", 0.0) or 0.0)
        if abs(stagger_value) < 0.05:
            stagger_value = 0.0
            stagger_axis = None
        else:
            stagger_axis = "columns"

    # Reorder all target-aligned arrays into canonical row-major (j, then i)
    # order. This keeps logical identities stable across finite/FFT paths.
    order = np.lexsort((logical[:, 0], logical[:, 1]))
    logical = logical[order]
    expected_rc = expected_rc[order]
    measured_rc = measured_rc[order]
    matched = np.asarray(registration.matched_mask, dtype=bool)[order]

    origin_index = np.flatnonzero((logical[:, 0] == 0) & (logical[:, 1] == 0))
    origin = expected_rc[int(origin_index[0])] if origin_index.size else expected_rc[0]
    diagnostics = dict(registration.diagnostics)
    lattice_size_source = str(diagnostics.get("lattice_size_source") or "")
    diagnostics.update({
        "grid_size_source": str(grid_size_source) or (
            "auto" if lattice_size_source == "image" else
            "manual" if lattice_size_source == "provided" else lattice_size_source
        ),
        "crop_coord": tuple(int(v) for v in crop_full),
        "crop_shape": tuple(int(v) for v in registration.detections.cropped_image.shape),
        "detected_count": int(registration.detections.positions_px.shape[1]),
        "matched_count": int(np.count_nonzero(matched)),
        "missing_count": int(np.count_nonzero(~matched)),
        "fit_rms_px": float(registration.rms_residual_px),
        "registration_mode": str(registration.diagnostics.get("search_path", "")),
        "stagger_source": str(registration.model.diagnostics.get("stagger_source", "")),
        "backend": "slmcore-compatible",
    })
    return GridLocalizationResult(
        origin_rc=np.asarray(origin, dtype=np.float64),
        basis_x_rc=basis_x_rc,
        basis_y_rc=basis_y_rc,
        n_cols=n_cols,
        n_rows=n_rows,
        stagger_axis=stagger_axis,
        stagger_fraction=float(stagger_value),
        grid_points_rc=expected_rc,
        logical_indices=logical,
        detected_points_rc=detected_rc,
        matched_mask=matched,
        measured_points_rc=measured_rc,
        num_cols=int(image_shape[1]),
        num_rows=int(image_shape[0]),
        diagnostics=diagnostics,
    )


def _run_slmcore_compatible(
    image: np.ndarray,
    *,
    n_cols: int | None,
    n_rows: int | None,
    stagger: float | None,
    detection_options: SpotDetectionOptions,
    registration_options: LatticeRegistrationOptions,
    transposed: bool = False,
) -> GridLocalizationResult:
    working = image.T if transposed else image
    expected_period = registration_options.expected_period_px
    if transposed and expected_period is not None:
        registration_options = replace(
            registration_options,
            expected_period_px=(expected_period[1], expected_period[0]),
        )
    try:
        detections = detect_spots(
            working,
            detection_options,
            expected_period_px=registration_options.expected_period_px,
        )
    except Exception as error:
        raise GridLocalizationError(str(error)) from error

    full_detected = _full_xy(detections.positions_px, detections.crop_coord)
    if transposed:
        detected_rc = full_detected.T
        y0, y1, x0, x1 = detections.crop_coord
        crop_coord = (x0, x1, y0, y1)
    else:
        detected_rc = full_detected[[1, 0], :].T
        crop_coord = detections.crop_coord
    failure_diagnostics = {
        "crop_coord": tuple(int(v) for v in crop_coord),
        "crop_shape": tuple(int(v) for v in detections.cropped_image.shape),
        "detected_count": int(detections.positions_px.shape[1]),
        "period_guess_px": registration_options.expected_period_px,
        "backend": "slmcore-compatible",
    }

    try:
        if n_cols is None:
            (count_x, count_y), size_inference = infer_lattice_shape(detections)
            indices = rectangular_lattice_indices(count_x, count_y)
        else:
            count_x = int(n_rows if transposed else n_cols)
            count_y = int(n_cols if transposed else n_rows)
            size_inference = None
            indices = rectangular_lattice_indices(count_x, count_y)
        model = make_lattice_model(indices, stagger=stagger)
        model_diagnostics = dict(model.diagnostics)
        model_diagnostics.update({
            "lattice_count_x": int(count_x),
            "lattice_count_y": int(count_y),
            "lattice_size_source": "image" if size_inference is not None else "provided",
            "lattice_size_inference": size_inference,
        })
        model = type(model)(
            lattice_indices=model.lattice_indices,
            logical_positions=model.logical_positions,
            representation=model.representation,
            diagnostics=model_diagnostics,
        )
        result = register_lattice(detections, model, registration_options)
    except Exception as error:
        raise GridLocalizationError(
            str(error),
            detected_points_rc=detected_rc,
            crop_coord=tuple(int(v) for v in crop_coord),
            diagnostics=failure_diagnostics,
        ) from error

    return _grid_from_registration(
        image.shape,
        result,
        transposed=transposed,
        grid_size_source="auto" if n_cols is None else "manual",
    )


def _candidate_score(result: GridLocalizationResult) -> tuple[int, float]:
    return (int(result.matched_count), -float(result.diagnostics.get("fit_rms_px", np.inf)))


def localize_grid(
    img_data: np.ndarray,
    *,
    xp_guess: float = 10.0,
    yp_guess: float = 10.0,
    n_cols: int | None = None,
    n_rows: int | None = None,
    crop_threshold: float = 0.4,
    crop_margin_px: int = 1,
    spot_threshold_rel: float = 0.10,
    spot_blur_sigma: float = 1.0,
    focus_localization_method: str = "cog",
    focus_search_window_px: int = 2,
    spot_min_distance_fraction: float = 0.35,
    registration_mode: str = "auto",
    period_tolerance_fraction: float = 0.25,
    fft_exclude_fraction: float = 0.03,
    fft_peak_count: int = 14,
    lattice_candidate_search_mode: str = "fast",
    matching_gate_fraction: float = 0.40,
    robust_outlier_sigma: float = 3.5,
    max_registration_iterations: int = 6,
    min_match_fraction: float = 0.35,
    use_period_prior: bool = True,
    stagger_mode: str = "auto",
    stagger_axis: str = "auto",
    stagger_fraction: float = 0.5,
    # Compatibility aliases from the pre-SLMcore-compatible ImControl API.
    threshold_rel: float | None = None,
    gaussian_sigma: float | None = None,
    min_distance_fraction: float | None = None,
) -> GridLocalizationResult:
    """Localize a finite 2-D grid with SLMcore-compatible numerical behavior.

    The public ImControl API is retained, but crop/detection, finite topology,
    stagger inference, robust affine refinement and FFT fallback now follow the
    same implementation as the SLMcore localizer.
    """
    # SLMcore parameter names are the canonical public API. Keep the three
    # names used by the earlier ImControl prototype as aliases so existing
    # callers/scripts do not break while both uLenses and Transform Calibration
    # consume the shared parameter contract directly.
    if threshold_rel is not None:
        spot_threshold_rel = float(threshold_rel)
    if gaussian_sigma is not None:
        spot_blur_sigma = float(gaussian_sigma)
    if min_distance_fraction is not None:
        spot_min_distance_fraction = float(min_distance_fraction)

    image = _as_grid_image(img_data)
    xp_guess = float(xp_guess)
    yp_guess = float(yp_guess)
    if xp_guess <= 0 or yp_guess <= 0:
        raise ValueError("grid period guesses must be positive")
    if (n_cols is None) != (n_rows is None):
        raise ValueError("n_cols and n_rows must be supplied together")
    if n_cols is not None and (int(n_cols) < 2 or int(n_rows) < 2):
        raise ValueError("finite grid counts must both be at least 2")
    stagger_mode = str(stagger_mode).lower()
    stagger_axis = str(stagger_axis).lower()
    if stagger_mode not in ("auto", "none", "manual"):
        raise ValueError("stagger_mode must be 'auto', 'none', or 'manual'")
    if stagger_axis not in ("auto", "rows", "columns"):
        raise ValueError("stagger_axis must be 'auto', 'rows', or 'columns'")
    if stagger_mode == "manual" and stagger_axis == "auto":
        raise ValueError("manual stagger requires stagger_axis rows or columns")
    if not 0.0 <= float(stagger_fraction) <= 1.0:
        raise ValueError("stagger_fraction must be within [0, 1]")

    detection_options, registration_options = _backend_options(
        xp_guess=xp_guess,
        yp_guess=yp_guess,
        use_period_prior=bool(use_period_prior),
        crop_threshold=crop_threshold,
        crop_margin_px=crop_margin_px,
        threshold_rel=spot_threshold_rel,
        gaussian_sigma=spot_blur_sigma,
        min_distance_fraction=spot_min_distance_fraction,
        focus_localization_method=focus_localization_method,
        focus_search_window_px=focus_search_window_px,
        registration_mode=registration_mode,
        period_tolerance_fraction=period_tolerance_fraction,
        fft_exclude_fraction=fft_exclude_fraction,
        fft_peak_count=fft_peak_count,
        lattice_candidate_search_mode=lattice_candidate_search_mode,
        matching_gate_fraction=matching_gate_fraction,
        robust_outlier_sigma=robust_outlier_sigma,
        max_registration_iterations=max_registration_iterations,
        min_match_fraction=min_match_fraction,
    )

    stagger = None if stagger_mode == "auto" else (0.0 if stagger_mode == "none" else float(stagger_fraction))
    if stagger_mode == "manual" and stagger_axis == "columns":
        return _run_slmcore_compatible(
            image, n_cols=n_cols, n_rows=n_rows, stagger=stagger,
            detection_options=detection_options, registration_options=registration_options,
            transposed=True,
        )

    if stagger_mode == "auto" and stagger_axis == "columns":
        return _run_slmcore_compatible(
            image, n_cols=n_cols, n_rows=n_rows, stagger=None,
            detection_options=detection_options, registration_options=registration_options,
            transposed=True,
        )

    primary_error = None
    try:
        primary = _run_slmcore_compatible(
            image, n_cols=n_cols, n_rows=n_rows, stagger=stagger,
            detection_options=detection_options, registration_options=registration_options,
            transposed=False,
        )
    except GridLocalizationError as error:
        primary = None
        primary_error = error

    # SLMcore's canonical stagger family is alternating rows. For ImControl's
    # historical "auto axis" compatibility, also evaluate the exact same
    # backend on the transposed image and keep the stronger measured fit.
    if stagger_mode == "auto" and stagger_axis == "auto":
        try:
            secondary = _run_slmcore_compatible(
                image, n_cols=n_cols, n_rows=n_rows, stagger=None,
                detection_options=detection_options, registration_options=registration_options,
                transposed=True,
            )
        except GridLocalizationError:
            secondary = None
        if primary is None and secondary is not None:
            return secondary
        if primary is not None and secondary is not None and _candidate_score(secondary) > _candidate_score(primary):
            return secondary

    if primary is not None:
        return primary
    assert primary_error is not None
    raise primary_error


def refine_grid_foci(
    img_data: np.ndarray,
    grid: GridLocalizationResult,
    *,
    search_radius_px: int = 5,
    gaussian_sigma: float = 1.0,
    matched_only: bool = False,
) -> np.ndarray:
    """Compatibility focus refinement around fitted grid sites.

    The SLMcore-compatible detector already returns subpixel measured positions.
    For matched sites this function therefore returns those measured positions;
    predicted-site local searching is retained only for legacy callers asking to
    refine every site.
    """
    if matched_only:
        return np.asarray(grid.measured_points_rc[grid.matched_mask], dtype=np.float64)
    image = _as_grid_image(img_data)
    radius = int(search_radius_px)
    if radius < 0:
        raise ValueError("search_radius_px must be non-negative")
    sigma = float(gaussian_sigma)
    if sigma < 0:
        raise ValueError("gaussian_sigma must be non-negative")
    search = gaussian_filter(image, sigma) if sigma else image
    return _refine_focus_sites(image, search, grid.grid_points_rc, radius)


def _refine_focus_sites(image: np.ndarray, search: np.ndarray, sites: np.ndarray, radius: int) -> np.ndarray:
    refined = []
    h, w = image.shape
    for predicted_row, predicted_col in np.asarray(sites, dtype=np.float64):
        center_row = int(round(float(predicted_row)))
        center_col = int(round(float(predicted_col)))
        r0 = max(0, center_row - radius); r1 = min(h, center_row + radius + 1)
        c0 = max(0, center_col - radius); c1 = min(w, center_col + radius + 1)
        if r0 >= r1 or c0 >= c1:
            raise RuntimeError("Predicted grid site lies outside the image; check grid localization")
        patch = search[r0:r1, c0:c1]
        peak_r, peak_c = np.unravel_index(int(np.argmax(patch)), patch.shape)
        peak_row = r0 + int(peak_r); peak_col = c0 + int(peak_c)
        com_radius = max(1, min(radius, 3)) if radius else 0
        rr0 = max(0, peak_row - com_radius); rr1 = min(h, peak_row + com_radius + 1)
        cc0 = max(0, peak_col - com_radius); cc1 = min(w, peak_col + com_radius + 1)
        local = image[rr0:rr1, cc0:cc1].astype(np.float64, copy=False)
        local = np.clip(local - np.percentile(local, 10), 0.0, None)
        total = float(np.sum(local))
        if total <= 0:
            refined.append((float(peak_row), float(peak_col))); continue
        row_coords = np.arange(rr0, rr1, dtype=np.float64)[:, None]
        col_coords = np.arange(cc0, cc1, dtype=np.float64)[None, :]
        refined.append((float(np.sum(local * row_coords) / total), float(np.sum(local * col_coords) / total)))
    return np.asarray(refined, dtype=np.float64)


def search_missing_grid_foci(
    img_data: np.ndarray,
    grid: GridLocalizationResult,
    *,
    search_radius_px: int = 5,
    gaussian_sigma: float = 1.0,
    threshold_rel: float = 0.05,
) -> GridLocalizationResult:
    """Explicitly search image evidence near currently missing predicted sites."""
    if grid.missing_count == 0:
        return grid
    image = _as_grid_image(img_data)
    radius = max(1, int(search_radius_px))
    sigma = max(0.0, float(gaussian_sigma))
    threshold = float(threshold_rel)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold_rel must be between 0 and 1")
    crop_coord = tuple(int(v) for v in grid.diagnostics.get("crop_coord", (0, image.shape[0], 0, image.shape[1])))
    y0, y1, x0, x1 = crop_coord
    cropped = image[y0:y1, x0:x1]
    processed = _preprocess(cropped, sigma)
    matched = np.asarray(grid.matched_mask, dtype=bool).copy()
    measured = np.asarray(grid.measured_points_rc, dtype=np.float64).copy()
    recovered = 0
    search_image = gaussian_filter(image, sigma) if sigma else image
    for index in np.flatnonzero(~matched):
        predicted_row, predicted_col = grid.grid_points_rc[index]
        center_row = int(round(float(predicted_row) - y0)); center_col = int(round(float(predicted_col) - x0))
        r0 = max(0, center_row - radius); r1 = min(processed.shape[0], center_row + radius + 1)
        c0 = max(0, center_col - radius); c1 = min(processed.shape[1], center_col + radius + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        patch = processed[r0:r1, c0:c1]
        peak_r, peak_c = np.unravel_index(int(np.argmax(patch)), patch.shape)
        if float(patch[peak_r, peak_c]) < threshold:
            continue
        site = np.asarray([[float(y0 + r0 + peak_r), float(x0 + c0 + peak_c)]])
        measured[index] = _refine_focus_sites(image, search_image, site, radius)[0]
        matched[index] = True
        recovered += 1
    if recovered == 0:
        return grid
    diagnostics = dict(grid.diagnostics)
    diagnostics.update({
        "matched_count": int(np.count_nonzero(matched)),
        "missing_count": int(np.count_nonzero(~matched)),
        "recovered_missing_count": int(diagnostics.get("recovered_missing_count", 0)) + int(recovered),
    })
    return replace(grid, matched_mask=matched, measured_points_rc=measured, diagnostics=diagnostics)


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

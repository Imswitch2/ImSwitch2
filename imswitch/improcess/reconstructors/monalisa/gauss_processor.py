"""Fast Gaussian-weighted reassignment processor for MoNaLISA live reconstruction."""

from typing import TYPE_CHECKING, Any

import numpy as np

from imswitch.imcommon.model import initLogger

from .scan_geometry import (
    get_1d_indices,
    get_center_coords,
    get_interp_coords,
    get_pinhole_footprint,
    get_rectangles_coords,
)

try:
    import cupy as cp

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None

if TYPE_CHECKING:
    if CUPY_AVAILABLE:
        import cupy as cp


DEFAULT_FOOTPRINT_NUM_RECTS = 3
DEFAULT_GAUSSIAN_SIGMA_PX = 2.0
DEFAULT_PINHOLE_RADIUS_SIGMA = 1.5

SAMPLING_MODE_BILINEAR = "bilinear"
SAMPLING_MODE_EXACT = "exact"


def normalize_sampling_mode(mode: object) -> str:
    """Normalize a sampling-mode value ('bilinear' default).

    ``bilinear`` is the legacy path: footprint offsets are integers relative
    to the *fractional* focus center, so every sample needs bilinear
    interpolation — which low-passes the peak and biases the fitted amplitude
    by several percent, varying with each focus' subpixel phase. ``exact``
    samples the real integer pixels around each focus and fits with
    per-focus weights evaluated at the true offsets: no interpolation, no
    phase-dependent bias, and one gather instead of four.
    """
    if mode is None:
        return SAMPLING_MODE_BILINEAR
    text = str(mode).strip().lower()
    if not text:
        return SAMPLING_MODE_BILINEAR
    if "exact" in text:
        return SAMPLING_MODE_EXACT
    if "bilinear" in text or "legacy" in text:
        return SAMPLING_MODE_BILINEAR
    raise ValueError(
        f"Unknown fast-Gauss sampling mode {mode!r}; "
        f"use '{SAMPLING_MODE_BILINEAR}' or '{SAMPLING_MODE_EXACT}'"
    )


def validate_gaussian_fit_options(
    num_rects: int | float | str | None = DEFAULT_FOOTPRINT_NUM_RECTS,
    gaussian_sigma_px: int | float | str | None = DEFAULT_GAUSSIAN_SIGMA_PX,
    pinhole_radius_px: int | float | str | None = None,
) -> tuple[int, float, float | None]:
    """Validate and normalize fast-Gauss footprint options.

    ``pinhole_radius_px`` is optional: when ``None`` (or non-positive) the
    legacy ``num_rects`` shell footprint is used; when positive it selects a
    circular pinhole footprint of that radius (which then governs the
    footprint instead of ``num_rects``).
    """
    if num_rects is None:
        num_rects = DEFAULT_FOOTPRINT_NUM_RECTS
    if gaussian_sigma_px is None:
        gaussian_sigma_px = DEFAULT_GAUSSIAN_SIGMA_PX

    num_rects = int(num_rects)
    gaussian_sigma_px = float(gaussian_sigma_px)
    if num_rects < 1:
        raise ValueError("num_rects must be >= 1")
    if gaussian_sigma_px <= 0:
        raise ValueError("gaussian_sigma_px must be > 0")

    if pinhole_radius_px is not None:
        pinhole_radius_px = float(pinhole_radius_px)
        if pinhole_radius_px <= 0:
            pinhole_radius_px = None  # treat non-positive as "no pinhole"
    return num_rects, gaussian_sigma_px, pinhole_radius_px


def calculate_gaussian_lsq_weights(
    x_offsets: np.ndarray,
    y_offsets: np.ndarray,
    gaussian_sigma_px: float,
    fit_background: bool = True,
) -> np.ndarray:
    """
    Build weights that project footprint samples onto a Gaussian amplitude.

    With ``fit_background`` the fitted model is
    ``sample = amplitude * gaussian + constant_background`` and the returned
    vector extracts the amplitude term. With ``fit_background=False`` (or a
    footprint too small to also fit a background, i.e. < 2 samples) it reduces
    to a pure Gaussian matched filter ``amplitude = (g·sample)/(g·g)``.
    """
    gauss_vec = np.exp(
        -((x_offsets**2 + y_offsets**2) / (2 * gaussian_sigma_px**2))
    )
    rows = [gauss_vec]
    if fit_background and len(gauss_vec) >= 2:
        rows.append(np.ones(len(gauss_vec)))
    design = np.stack(rows)
    return np.linalg.pinv(design)[:, 0]


def _build_footprint(
    num_rects: int,
    pinhole_radius_px: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Footprint offsets shared by the interp coords and the fit weights.

    A circular pinhole (``pinhole_radius_px``) takes precedence over the legacy
    ``num_rects`` shell footprint so both consumers sample the exact same set.
    """
    if pinhole_radius_px is not None:
        return get_pinhole_footprint(pinhole_radius_px)
    return get_rectangles_coords(num_rects)


def build_exact_sampling(
    centers_x: np.ndarray,
    centers_y: np.ndarray,
    footprint: tuple[np.ndarray, np.ndarray],
    gaussian_sigma_px: float,
    fit_background: bool,
    num_rows: int,
    num_cols: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Integer pixel sets and per-focus LSQ weights for exact sampling.

    For each (fractional) focus center, the footprint offsets are anchored to
    the nearest integer pixel and the Gaussian+background model is evaluated
    at the *true* pixel-minus-center offsets, so the fit matches the samples
    without any interpolation. Out-of-frame pixels are masked out of the fit
    (weight 0) rather than clamped onto border pixels, so edge foci get a
    proper least squares over the pixels that exist. The amplitude weights
    come from the closed-form 2x2 normal equations of ``a*g + b*1``; a focus
    left with fewer than two usable pixels falls back to the pure matched
    filter, and one with no usable pixels gets zero weights.

    Returns:
        ``(pixel_rows, pixel_cols, weights)``, each of shape
        ``(num_foci, pts_per_focus)`` — apply as
        ``(frame[pixel_rows, pixel_cols] * weights).sum(axis=-1)``.
    """
    centers_x = np.asarray(centers_x, dtype=float)
    centers_y = np.asarray(centers_y, dtype=float)
    offsets_x = np.round(np.asarray(footprint[0], dtype=float)).astype(np.int64)
    offsets_y = np.round(np.asarray(footprint[1], dtype=float)).astype(np.int64)

    anchor_x = np.round(centers_x).astype(np.int64)
    anchor_y = np.round(centers_y).astype(np.int64)
    pixel_cols = anchor_x[:, None] + offsets_x[None, :]
    pixel_rows = anchor_y[:, None] + offsets_y[None, :]

    in_frame = (
        (pixel_cols >= 0)
        & (pixel_cols < num_cols)
        & (pixel_rows >= 0)
        & (pixel_rows < num_rows)
    )
    pixel_cols = np.clip(pixel_cols, 0, num_cols - 1)
    pixel_rows = np.clip(pixel_rows, 0, num_rows - 1)

    true_dx = pixel_cols - centers_x[:, None]
    true_dy = pixel_rows - centers_y[:, None]
    gauss = np.exp(
        -((true_dx**2 + true_dy**2) / (2 * float(gaussian_sigma_px) ** 2))
    )
    gauss = np.where(in_frame, gauss, 0.0)
    ones = in_frame.astype(float)

    sum_gg = (gauss * gauss).sum(axis=1)
    sum_g1 = gauss.sum(axis=1)
    sum_11 = ones.sum(axis=1)

    safe_gg = np.where(sum_gg > 0, sum_gg, 1.0)
    matched = gauss / safe_gg[:, None]
    matched = np.where(sum_gg[:, None] > 0, matched, 0.0)

    if not fit_background:
        return pixel_rows, pixel_cols, matched

    det = sum_gg * sum_11 - sum_g1**2
    usable = (det > 1e-12) & (sum_11 >= 2)
    safe_det = np.where(usable, det, 1.0)
    weights = (sum_11[:, None] * gauss - sum_g1[:, None] * ones) / safe_det[:, None]
    weights = np.where(usable[:, None], weights, matched)
    return pixel_rows, pixel_cols, weights


def extract_lattice_amplitudes(
    frames: np.ndarray,
    centers_x: np.ndarray,
    centers_y: np.ndarray,
    footprint: tuple[np.ndarray, np.ndarray],
    gaussian_sigma_px: float,
    fit_background: bool = True,
    chunk_frames: int = 256,
) -> np.ndarray:
    """Per-frame LSQ amplitudes at an arbitrary list of focus centers.

    The general-lattice reassignment path: focus centers come from lattice
    detection instead of the axis-aligned grid, so there is no shared-weight
    shortcut — every focus gets exact-pixel per-focus weights (see
    :func:`build_exact_sampling`). Processes ``frames`` in chunks to bound
    the transient gather memory.

    Returns:
        Array of shape ``(num_frames, num_foci)``.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3:
        raise ValueError(f"Expected 3D frames (N, Y, X), got shape {frames.shape}")
    num_frames, num_rows, num_cols = frames.shape
    pixel_rows, pixel_cols, weights = build_exact_sampling(
        centers_x, centers_y, footprint, gaussian_sigma_px,
        fit_background, num_rows, num_cols,
    )

    amplitudes = np.empty((num_frames, pixel_rows.shape[0]), dtype=np.float64)
    chunk_frames = max(1, int(chunk_frames))
    for start in range(0, num_frames, chunk_frames):
        stop = min(num_frames, start + chunk_frames)
        samples = frames[start:stop][:, pixel_rows, pixel_cols]
        amplitudes[start:stop] = (samples * weights).sum(axis=-1)
    return amplitudes


class GaussProcessorCPU:
    """
    CPU-based image reconstruction using Gaussian least-squares weighting and bilinear interpolation.

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_cols: Total number of columns in the frame.
        num_rows: Total number of rows in the frame.
        num_rects: Number of rectangles used to model the foci.
        gaussian_sigma_px: Gaussian sigma in footprint pixels.
        scan_ori: Scan orientation string (e.g. "+x+y").
    """

    def __init__(
        self,
        xp: float,
        xo: float,
        yp: float,
        yo: float,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        num_rows: int,
        num_cols: int,
        num_rects: int = DEFAULT_FOOTPRINT_NUM_RECTS,
        gaussian_sigma_px: float = DEFAULT_GAUSSIAN_SIGMA_PX,
        pinhole_radius_px: float | None = None,
        fit_background: bool = True,
        scan_ori: str = "+x+y",
        sampling_mode: str = SAMPLING_MODE_BILINEAR,
    ):
        num_rects, gaussian_sigma_px, pinhole_radius_px = validate_gaussian_fit_options(
            num_rects, gaussian_sigma_px, pinhole_radius_px
        )
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.num_foci = nx_c * ny_c
        self.num_rects = num_rects
        self.gaussian_sigma_px = gaussian_sigma_px
        self.pinhole_radius_px = pinhole_radius_px
        self.fit_background = fit_background
        self.scan_ori = scan_ori
        self.sampling_mode = normalize_sampling_mode(sampling_mode)
        footprint = _build_footprint(num_rects, pinhole_radius_px)
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            centers_x, centers_y = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
            self.pixel_rows, self.pixel_cols, self.exact_weights = (
                build_exact_sampling(
                    centers_x, centers_y, footprint, gaussian_sigma_px,
                    fit_background, num_rows, num_cols,
                )
            )
            self.pts_per_focus = self.pixel_rows.shape[1]
        else:
            self.x_interp, self.y_interp = get_interp_coords(
                xp, xo, yp, yo, nx_c, ny_c, num_rows, num_cols, num_rects,
                footprint=footprint,
            )
            self.lsq_weights, self.pts_per_focus = self._calculate_weights(
                footprint, gaussian_sigma_px, fit_background
            )
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)
        self.num_frames_in_stack = nx_s * ny_s

    def _calculate_weights(
        self,
        footprint: tuple[np.ndarray, np.ndarray],
        gaussian_sigma_px: float,
        fit_background: bool,
    ) -> tuple[np.ndarray, int]:
        """
        Calculate the least-squares weights based on a Gaussian profile.

        Args:
            footprint: ``(x_offsets, y_offsets)`` sampled around each focus.
            gaussian_sigma_px: Gaussian sigma in footprint pixels.
            fit_background: Whether to also fit a constant background term.

        Returns:
            Tuple containing the 1D weight array and the number of points per focus.
        """
        Xr, Yr = footprint
        lsq_weights = calculate_gaussian_lsq_weights(
            Xr, Yr, gaussian_sigma_px, fit_background
        )
        return np.array(lsq_weights), len(Xr)

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Extract per-focus amplitudes from one frame.

        Args:
            frame: The 2D raw image frame.

        Returns:
            1D array of reconstructed intensity values.
        """
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            samples = frame[self.pixel_rows, self.pixel_cols]
            return (samples * self.exact_weights).sum(axis=-1)

        x0 = np.clip(np.floor(self.x_interp).astype(np.int32), 0, self.num_cols - 1)
        x1 = np.clip(x0 + 1, 0, self.num_cols - 1)

        y0 = np.clip(np.floor(self.y_interp).astype(np.int32), 0, self.num_rows - 1)
        y1 = np.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            frame[y0, x0] * (1 - dx) * (1 - dy)
            + frame[y1, x0] * (1 - dx) * dy
            + frame[y0, x1] * dx * (1 - dy)
            + frame[y1, x1] * dx * dy
        ).reshape((self.num_foci, self.pts_per_focus))

        return np.dot(interp_vals, self.lsq_weights)

    def process_chunk(self, chunk: np.ndarray) -> np.ndarray:
        """
        Process an entire 3D chunk (num_frames, Y, X) at once.

        Args:
            chunk: A sub-stack (chunk) of raw frames.

        Returns:
            Processed pixels for each raw frame in the chunk.
        """
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            samples = chunk[:, self.pixel_rows, self.pixel_cols]
            return (samples * self.exact_weights).sum(axis=-1)

        x0 = np.clip(np.floor(self.x_interp).astype(np.int32), 0, self.num_cols - 1)
        x1 = np.clip(x0 + 1, 0, self.num_cols - 1)
        y0 = np.clip(np.floor(self.y_interp).astype(np.int32), 0, self.num_rows - 1)
        y1 = np.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            chunk[:, y0, x0] * (1 - dx) * (1 - dy)
            + chunk[:, y1, x0] * (1 - dx) * dy
            + chunk[:, y0, x1] * dx * (1 - dy)
            + chunk[:, y1, x1] * dx * dy
        ).reshape((-1, self.num_foci, self.pts_per_focus))

        return np.matmul(interp_vals, self.lsq_weights)

    def update_frame_inds(
        self,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        scan_ori: str,
    ):
        """
        Update the frame indices for the processor.

        Args:
            nx_c: Number of foci along x.
            ny_c: Number of foci along y.
            nx_s: Scanner steps along x.
            ny_s: Scanner steps along y.
            scan_ori: Scan orientation string.
        """
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)


class GaussProcessorGPU:
    """
    GPU-accelerated image reconstruction using Gaussian least-squares weighting and bilinear interpolation.

    Requires CuPy. Falls back to CPU if unavailable.

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_cols: Total number of columns in the frame.
        num_rows: Total number of rows in the frame.
        num_rects: Number of rectangles used to model the foci.
        gaussian_sigma_px: Gaussian sigma in footprint pixels.
        scan_ori: Scan orientation string (e.g. "+x+y").
    """

    def __init__(
        self,
        xp: float,
        xo: float,
        yp: float,
        yo: float,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        num_rows: int,
        num_cols: int,
        num_rects: int = DEFAULT_FOOTPRINT_NUM_RECTS,
        gaussian_sigma_px: float = DEFAULT_GAUSSIAN_SIGMA_PX,
        pinhole_radius_px: float | None = None,
        fit_background: bool = True,
        scan_ori: str = "+x+y",
        sampling_mode: str = SAMPLING_MODE_BILINEAR,
    ):
        num_rects, gaussian_sigma_px, pinhole_radius_px = validate_gaussian_fit_options(
            num_rects, gaussian_sigma_px, pinhole_radius_px
        )
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.num_foci = nx_c * ny_c
        self.num_rects = num_rects
        self.gaussian_sigma_px = gaussian_sigma_px
        self.pinhole_radius_px = pinhole_radius_px
        self.fit_background = fit_background
        self.scan_ori = scan_ori
        self.sampling_mode = normalize_sampling_mode(sampling_mode)
        footprint = _build_footprint(num_rects, pinhole_radius_px)
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            centers_x, centers_y = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
            pixel_rows, pixel_cols, exact_weights = build_exact_sampling(
                centers_x, centers_y, footprint, gaussian_sigma_px,
                fit_background, num_rows, num_cols,
            )
            self.pixel_rows = cp.array(pixel_rows)
            self.pixel_cols = cp.array(pixel_cols)
            self.exact_weights = cp.array(exact_weights)
            self.pts_per_focus = pixel_rows.shape[1]
        else:
            x_interp, y_interp = get_interp_coords(
                xp, xo, yp, yo, nx_c, ny_c, num_rows, num_cols, num_rects,
                footprint=footprint,
            )
            self.x_interp = cp.array(x_interp)
            self.y_interp = cp.array(y_interp)
            self.lsq_weights, self.pts_per_focus = self._calculate_weights(
                footprint, gaussian_sigma_px, fit_background
            )
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)
        self.num_frames_in_stack = nx_s * ny_s

    def _calculate_weights(
        self,
        footprint: tuple[np.ndarray, np.ndarray],
        gaussian_sigma_px: float,
        fit_background: bool,
    ) -> tuple:
        """
        Calculate the least-squares weights based on a Gaussian profile.

        Args:
            footprint: ``(x_offsets, y_offsets)`` sampled around each focus.
            gaussian_sigma_px: Gaussian sigma in footprint pixels.
            fit_background: Whether to also fit a constant background term.

        Returns:
            Tuple containing the 1D weight array (CuPy) and the number of points per focus.
        """
        Xr, Yr = footprint
        lsq_weights = calculate_gaussian_lsq_weights(
            Xr, Yr, gaussian_sigma_px, fit_background
        )
        return cp.array(lsq_weights), len(Xr)

    def process_frame(self, frame_gpu: Any) -> np.ndarray:
        """
        Extract per-focus amplitudes from one frame (on GPU).

        Args:
            frame_gpu: The 2D raw image frame (already on GPU).

        Returns:
            1D array of reconstructed intensity values on CPU.
        """
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            samples = frame_gpu[self.pixel_rows, self.pixel_cols]
            return cp.asnumpy((samples * self.exact_weights).sum(axis=-1))

        x0 = cp.clip(cp.floor(self.x_interp).astype(cp.int32), 0, self.num_cols - 1)
        x1 = cp.clip(x0 + 1, 0, self.num_cols - 1)

        y0 = cp.clip(cp.floor(self.y_interp).astype(cp.int32), 0, self.num_rows - 1)
        y1 = cp.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            frame_gpu[y0, x0] * (1 - dx) * (1 - dy)
            + frame_gpu[y1, x0] * (1 - dx) * dy
            + frame_gpu[y0, x1] * dx * (1 - dy)
            + frame_gpu[y1, x1] * dx * dy
        ).reshape((self.num_foci, self.pts_per_focus))

        return cp.asnumpy(cp.dot(interp_vals, self.lsq_weights))

    def process_chunk(self, chunk_gpu: Any) -> np.ndarray:
        """
        Process an entire 3D chunk (num_frames, Y, X) at once on the GPU.

        Args:
            chunk_gpu: A sub-stack (chunk) of raw frames on GPU.

        Returns:
            Processed pixels for each raw frame in the chunk (on CPU).
        """
        if self.sampling_mode == SAMPLING_MODE_EXACT:
            samples = chunk_gpu[:, self.pixel_rows, self.pixel_cols]
            return cp.asnumpy((samples * self.exact_weights).sum(axis=-1))

        x0 = cp.clip(cp.floor(self.x_interp).astype(cp.int32), 0, self.num_cols - 1)
        x1 = cp.clip(x0 + 1, 0, self.num_cols - 1)
        y0 = cp.clip(cp.floor(self.y_interp).astype(cp.int32), 0, self.num_rows - 1)
        y1 = cp.clip(y0 + 1, 0, self.num_rows - 1)

        dx = self.x_interp - x0
        dy = self.y_interp - y0

        interp_vals = (
            chunk_gpu[:, y0, x0] * (1 - dx) * (1 - dy)
            + chunk_gpu[:, y1, x0] * (1 - dx) * dy
            + chunk_gpu[:, y0, x1] * dx * (1 - dy)
            + chunk_gpu[:, y1, x1] * dx * dy
        ).reshape((-1, self.num_foci, self.pts_per_focus))

        return cp.asnumpy(cp.matmul(interp_vals, self.lsq_weights))

    def update_frame_inds(
        self,
        nx_c: int,
        ny_c: int,
        nx_s: int,
        ny_s: int,
        scan_ori: str,
    ):
        """
        Update the frame indices for the processor.

        Args:
            nx_c: Number of foci along x.
            ny_c: Number of foci along y.
            nx_s: Scanner steps along x.
            ny_s: Scanner steps along y.
            scan_ori: Scan orientation string.
        """
        self.frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori)


def make_gauss_processor(
    xp: float,
    xo: float,
    yp: float,
    yo: float,
    nx_c: int,
    ny_c: int,
    nx_s: int,
    ny_s: int,
    num_rows: int,
    num_cols: int,
    num_rects: int = DEFAULT_FOOTPRINT_NUM_RECTS,
    gaussian_sigma_px: float = DEFAULT_GAUSSIAN_SIGMA_PX,
    pinhole_radius_px: float | None = None,
    fit_background: bool = True,
    scan_ori: str = "+x+y",
    sampling_mode: str = SAMPLING_MODE_BILINEAR,
    use_gpu: bool = False,
) -> GaussProcessorCPU | GaussProcessorGPU:
    """
    Factory function to create a Gauss processor (CPU or GPU).

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        num_rows: Total number of rows in the frame.
        num_cols: Total number of columns in the frame.
        num_rects: Number of rectangular shells (legacy footprint; used when
            ``pinhole_radius_px`` is None).
        gaussian_sigma_px: Gaussian sigma in footprint pixels.
        pinhole_radius_px: Optional circular pinhole radius (px). When > 0 it
            governs the footprint instead of ``num_rects``.
        fit_background: Whether to also fit a constant background term.
        scan_ori: Scan orientation string.
        sampling_mode: ``"bilinear"`` (legacy, shared weights on interpolated
            samples) or ``"exact"`` (per-focus weights on true integer
            pixels; unbiased and cheaper — see
            :func:`normalize_sampling_mode`).
        use_gpu: If True, attempt to use GPU; falls back to CPU if unavailable.

    Returns:
        A GaussProcessor instance (GPU if requested and available, otherwise CPU).
    """
    logger = initLogger("make_gauss_processor")
    num_rects, gaussian_sigma_px, pinhole_radius_px = validate_gaussian_fit_options(
        num_rects, gaussian_sigma_px, pinhole_radius_px
    )
    sampling_mode = normalize_sampling_mode(sampling_mode)
    args = (
        xp, xo, yp, yo, nx_c, ny_c, nx_s, ny_s, num_rows, num_cols,
        num_rects, gaussian_sigma_px, pinhole_radius_px, fit_background,
        scan_ori, sampling_mode,
    )

    if use_gpu and CUPY_AVAILABLE:
        logger.info("Creating GPU Gauss processor")
        return GaussProcessorGPU(*args)
    elif use_gpu and not CUPY_AVAILABLE:
        logger.info("GPU requested but CuPy not available; falling back to CPU")
        return GaussProcessorCPU(*args)
    else:
        return GaussProcessorCPU(*args)


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

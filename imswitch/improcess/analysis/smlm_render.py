"""Pure-numpy super-resolution rendering of a localization table.

Turns a localization coordinate table into an *image* (2D) or *volume* (3D) so
the existing ImProcess napari viewer can display it — no napari points layer
needed. Two render modes, matching the SMLM convention (and the pyMINFLUX
``render_xy``/``render_xyz`` interface the port plan references):

* ``"histogram"`` — count localizations per output bin. Sharp, unfiltered.
* ``"fixed_gaussian"`` — splat each localization as a Gaussian of a fixed
  FWHM. Smooth, mass-preserving (each point contributes unit total weight).

This is a clean-room implementation from the standard binning/splatting
algorithm (the plan notes pyMINFLUX is Apache-2.0 and ImSwitch is GPL, so
nothing is copied). Inputs are nm coordinates plus an output pixel size in
nm/px; outputs carry the world-coordinate grid so callers can set nm axis
scales.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: FWHM = FWHM_TO_SIGMA * sigma for a Gaussian.
FWHM_TO_SIGMA = 2.354820045


@dataclass(frozen=True)
class RenderGrid:
    """World-coordinate description of a rendered image/volume.

    ``origin_nm`` is the nm coordinate of the first bin's lower edge for each
    axis (in image order: y, x for 2D; z, y, x for 3D). ``pixel_size_nm`` is
    the bin size per axis, matching napari's ``scale``.
    """

    origin_nm: tuple[float, ...]
    pixel_size_nm: tuple[float, ...]
    shape: tuple[int, ...]


def _resolve_bounds(values: np.ndarray, lo: float | None, hi: float | None) -> tuple[float, float]:
    if lo is None:
        lo = float(np.min(values)) if values.size else 0.0
    if hi is None:
        hi = float(np.max(values)) if values.size else 0.0
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


def _axis_bins(span: float, pixel_size_nm: float) -> int:
    return max(1, int(np.ceil(span / pixel_size_nm)))


def render_xy(
    x_nm: np.ndarray,
    y_nm: np.ndarray,
    *,
    pixel_size_nm: float,
    render_type: str = "histogram",
    fwhm_nm: float = 20.0,
    weights: np.ndarray | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[np.ndarray, RenderGrid]:
    """Render x/y localizations (nm) into a 2D image.

    Parameters
    ----------
    x_nm, y_nm:
        Localization coordinates in nm.
    pixel_size_nm:
        Output (super-res) pixel size in nm/px, e.g. 5-10.
    render_type:
        ``"histogram"`` or ``"fixed_gaussian"``.
    fwhm_nm:
        Gaussian FWHM in nm (only for ``"fixed_gaussian"``).
    weights:
        Optional per-localization weight (e.g. photons). Defaults to unit.
    bounds:
        Optional ``(x_min, x_max, y_min, y_max)`` in nm; inferred from the data
        when omitted.

    Returns
    -------
    (image, grid) with ``image`` shape ``(Y, X)`` float32 and a
    :class:`RenderGrid` describing nm origin/scale.
    """
    if pixel_size_nm <= 0:
        raise ValueError("pixel_size_nm must be positive")
    x_nm = np.asarray(x_nm, dtype=np.float64).ravel()
    y_nm = np.asarray(y_nm, dtype=np.float64).ravel()
    if x_nm.shape != y_nm.shape:
        raise ValueError("x_nm and y_nm must have the same length")

    if bounds is not None:
        x_min, x_max, y_min, y_max = bounds
    else:
        x_min, x_max = _resolve_bounds(x_nm, None, None)
        y_min, y_max = _resolve_bounds(y_nm, None, None)
    x_min, x_max = _resolve_bounds(x_nm, x_min, x_max)
    y_min, y_max = _resolve_bounds(y_nm, y_min, y_max)

    width = _axis_bins(x_max - x_min, pixel_size_nm)
    height = _axis_bins(y_max - y_min, pixel_size_nm)
    grid = RenderGrid(
        origin_nm=(y_min, x_min),
        pixel_size_nm=(pixel_size_nm, pixel_size_nm),
        shape=(height, width),
    )

    if weights is not None:
        weights = np.asarray(weights, dtype=np.float64).ravel()

    col = (x_nm - x_min) / pixel_size_nm
    row = (y_nm - y_min) / pixel_size_nm

    if render_type == "histogram":
        image = _histogram2d(row, col, height, width, weights)
    elif render_type == "fixed_gaussian":
        sigma_px = (fwhm_nm / FWHM_TO_SIGMA) / pixel_size_nm
        image = _gaussian_splat2d(row, col, height, width, sigma_px, weights)
    else:
        raise ValueError(f"Unknown render_type {render_type!r}")

    return image.astype(np.float32), grid


def render_xyz(
    x_nm: np.ndarray,
    y_nm: np.ndarray,
    z_nm: np.ndarray,
    *,
    pixel_size_nm: float,
    z_pixel_size_nm: float | None = None,
    render_type: str = "histogram",
    fwhm_nm: float = 20.0,
    fwhm_z_nm: float | None = None,
    weights: np.ndarray | None = None,
    bounds: tuple[float, float, float, float, float, float] | None = None,
) -> tuple[np.ndarray, RenderGrid]:
    """Render x/y/z localizations (nm) into a 3D volume ``(Z, Y, X)``.

    ``z_pixel_size_nm`` defaults to ``pixel_size_nm``; ``fwhm_z_nm`` defaults to
    ``fwhm_nm``. Semantics otherwise mirror :func:`render_xy`.
    """
    if pixel_size_nm <= 0:
        raise ValueError("pixel_size_nm must be positive")
    z_pixel_size_nm = float(z_pixel_size_nm) if z_pixel_size_nm else float(pixel_size_nm)
    fwhm_z_nm = float(fwhm_z_nm) if fwhm_z_nm is not None else float(fwhm_nm)

    x_nm = np.asarray(x_nm, dtype=np.float64).ravel()
    y_nm = np.asarray(y_nm, dtype=np.float64).ravel()
    z_nm = np.asarray(z_nm, dtype=np.float64).ravel()
    if not (x_nm.shape == y_nm.shape == z_nm.shape):
        raise ValueError("x_nm, y_nm, z_nm must have the same length")

    if bounds is not None:
        x_min, x_max, y_min, y_max, z_min, z_max = bounds
    else:
        x_min = x_max = y_min = y_max = z_min = z_max = None
    x_min, x_max = _resolve_bounds(x_nm, x_min, x_max)
    y_min, y_max = _resolve_bounds(y_nm, y_min, y_max)
    z_min, z_max = _resolve_bounds(z_nm, z_min, z_max)

    width = _axis_bins(x_max - x_min, pixel_size_nm)
    height = _axis_bins(y_max - y_min, pixel_size_nm)
    depth = _axis_bins(z_max - z_min, z_pixel_size_nm)
    grid = RenderGrid(
        origin_nm=(z_min, y_min, x_min),
        pixel_size_nm=(z_pixel_size_nm, pixel_size_nm, pixel_size_nm),
        shape=(depth, height, width),
    )

    if weights is not None:
        weights = np.asarray(weights, dtype=np.float64).ravel()

    plane = (z_nm - z_min) / z_pixel_size_nm
    row = (y_nm - y_min) / pixel_size_nm
    col = (x_nm - x_min) / pixel_size_nm

    if render_type == "histogram":
        volume = _histogram3d(plane, row, col, depth, height, width, weights)
    elif render_type == "fixed_gaussian":
        sigma_xy = (fwhm_nm / FWHM_TO_SIGMA) / pixel_size_nm
        sigma_z = (fwhm_z_nm / FWHM_TO_SIGMA) / z_pixel_size_nm
        volume = _gaussian_splat3d(
            plane, row, col, depth, height, width, sigma_z, sigma_xy, weights
        )
    else:
        raise ValueError(f"Unknown render_type {render_type!r}")

    return volume.astype(np.float32), grid


# -- binning ---------------------------------------------------------------


def _histogram2d(row, col, height, width, weights) -> np.ndarray:
    image = np.zeros((height, width), dtype=np.float64)
    r = np.clip(np.floor(row).astype(np.intp), 0, height - 1)
    c = np.clip(np.floor(col).astype(np.intp), 0, width - 1)
    np.add.at(image, (r, c), 1.0 if weights is None else weights)
    return image


def _histogram3d(plane, row, col, depth, height, width, weights) -> np.ndarray:
    volume = np.zeros((depth, height, width), dtype=np.float64)
    p = np.clip(np.floor(plane).astype(np.intp), 0, depth - 1)
    r = np.clip(np.floor(row).astype(np.intp), 0, height - 1)
    c = np.clip(np.floor(col).astype(np.intp), 0, width - 1)
    np.add.at(volume, (p, r, c), 1.0 if weights is None else weights)
    return volume


# -- Gaussian splatting -----------------------------------------------------


def _kernel_radius(sigma_px: float) -> int:
    # 3-sigma covers >99.7% of the mass; clamp so a tiny sigma still splats.
    return max(1, int(np.ceil(3.0 * max(sigma_px, 1e-3))))


def _gaussian_splat2d(row, col, height, width, sigma_px, weights) -> np.ndarray:
    """Vectorized fixed-Gaussian splat.

    Each localization contributes a normalized 2D Gaussian evaluated over a
    small ``(2r+1)`` kernel window centred on its sub-pixel position, so the
    result is smooth and (up to edge clipping) mass-preserving.
    """
    image = np.zeros((height, width), dtype=np.float64)
    if row.size == 0:
        return image
    sigma_px = max(float(sigma_px), 1e-3)
    radius = _kernel_radius(sigma_px)
    offsets = np.arange(-radius, radius + 1)

    r0 = np.floor(row).astype(np.intp)
    c0 = np.floor(col).astype(np.intp)
    w = np.ones(row.shape) if weights is None else np.asarray(weights, dtype=np.float64)

    two_sigma_sq = 2.0 * sigma_px * sigma_px
    for dr in offsets:
        rr = r0 + dr
        dy = (rr + 0.5) - row  # distance from sub-pixel centre to bin centre
        gy = np.exp(-(dy * dy) / two_sigma_sq)
        valid_r = (rr >= 0) & (rr < height)
        for dc in offsets:
            cc = c0 + dc
            valid = valid_r & (cc >= 0) & (cc < width)
            if not np.any(valid):
                continue
            dx = (cc + 0.5) - col
            contribution = w * gy * np.exp(-(dx * dx) / two_sigma_sq)
            np.add.at(image, (rr[valid], cc[valid]), contribution[valid])

    # Normalize each point's total weight to unit (mass preservation) by
    # dividing by the analytic 2D Gaussian integral over the kernel.
    norm = 2.0 * np.pi * sigma_px * sigma_px
    return image / norm


def _gaussian_splat3d(
    plane, row, col, depth, height, width, sigma_z, sigma_xy, weights
) -> np.ndarray:
    volume = np.zeros((depth, height, width), dtype=np.float64)
    if row.size == 0:
        return volume
    sigma_z = max(float(sigma_z), 1e-3)
    sigma_xy = max(float(sigma_xy), 1e-3)
    radius_z = _kernel_radius(sigma_z)
    radius_xy = _kernel_radius(sigma_xy)

    p0 = np.floor(plane).astype(np.intp)
    r0 = np.floor(row).astype(np.intp)
    c0 = np.floor(col).astype(np.intp)
    w = np.ones(row.shape) if weights is None else np.asarray(weights, dtype=np.float64)

    two_sig_z = 2.0 * sigma_z * sigma_z
    two_sig_xy = 2.0 * sigma_xy * sigma_xy
    for dp in range(-radius_z, radius_z + 1):
        pp = p0 + dp
        valid_p = (pp >= 0) & (pp < depth)
        if not np.any(valid_p):
            continue
        dz = (pp + 0.5) - plane
        gz = np.exp(-(dz * dz) / two_sig_z)
        for dr in range(-radius_xy, radius_xy + 1):
            rr = r0 + dr
            valid_pr = valid_p & (rr >= 0) & (rr < height)
            if not np.any(valid_pr):
                continue
            dy = (rr + 0.5) - row
            gy = np.exp(-(dy * dy) / two_sig_xy)
            for dc in range(-radius_xy, radius_xy + 1):
                cc = c0 + dc
                valid = valid_pr & (cc >= 0) & (cc < width)
                if not np.any(valid):
                    continue
                dx = (cc + 0.5) - col
                contribution = w * gz * gy * np.exp(-(dx * dx) / two_sig_xy)
                np.add.at(volume, (pp[valid], rr[valid], cc[valid]), contribution[valid])

    norm = (2.0 * np.pi) ** 1.5 * sigma_z * sigma_xy * sigma_xy
    return volume / norm


__all__ = ["render_xy", "render_xyz", "RenderGrid", "FWHM_TO_SIGMA"]

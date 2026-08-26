"""GPU ISM reassignment kernel extracted from monalisa-xrecon.

This module intentionally contains only the computational path used by
``recon_reassign_ism``: linear microlens centering, optional per-patch mean
subtraction, Fourier-domain ISM shift, and Gaussian+constant least-squares fit.

The implementation is adapted for CuPy so the heavy reconstruction remains on
one CUDA device from the initial upload until the final 2-D image is returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

WORKING_FLOAT_DTYPE = np.float32
INDEX_DTYPE = np.int32


@dataclass(frozen=True)
class IsmGeometry:
    scan_steps: int
    camera_pixels: int
    pixels_per_ulens: int
    ulenses_per_axis: int
    output_pixels: int
    output_pixel_size_nm: float
    scanning_step_px: float


def _import_cupy():
    try:
        import cupy as cp
    except ImportError as exc:
        raise RuntimeError(
            "GPU ISM reassignment requires CuPy. Install the CUDA-matched "
            "package, e.g. `pip install cupy-cuda12x`, or use your existing "
            "ImProcess CUDA environment."
        ) from exc
    return cp


def gpu_available() -> bool:
    try:
        cp = _import_cupy()
        return cp.cuda.runtime.getDeviceCount() > 0
    except Exception:
        return False


def validate_raw_stack(raw_data) -> tuple[int, int]:
    shape = tuple(raw_data.shape)
    if len(shape) != 3:
        raise ValueError(
            "ISM reassignment expects a 3-D stack (M*M, camera_y, camera_x); "
            f"got {shape}"
        )
    if shape[1] != shape[2]:
        raise ValueError(
            "The current extracted xrecon path expects square camera frames; "
            f"got {shape[1:]}"
        )
    m = int(round(np.sqrt(shape[0])))
    if m * m != shape[0]:
        raise ValueError(
            "ISM reassignment expects a square scan: the frame count must be "
            f"M*M, got {shape[0]} frames"
        )
    return m, int(shape[1])


def patternfinder_to_xrecon(
    pattern: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    """Convert ImProcess PatternFinder output to xrecon period/phase.

    PatternFinder uses ``[row_offset, col_offset, row_period, col_period]`` in
    camera pixels.  xrecon uses ``period=[x, y]`` and fractional
    ``phase=[x, y]`` in the centering equation
    ``source=(index-phase)*period``.
    """
    values = np.asarray(pattern, dtype=np.float64)
    if values.shape != (4,):
        raise ValueError("pattern must contain row_offset, col_offset, row_period, col_period")
    row_offset, col_offset, row_period, col_period = values
    if not np.isfinite(values).all() or row_period <= 0 or col_period <= 0:
        raise ValueError(f"invalid rectangular pattern: {values.tolist()}")

    row_offset = np.mod(row_offset, row_period)
    col_offset = np.mod(col_offset, col_period)
    period = np.asarray([col_period, row_period], dtype=np.float64)
    phase = np.mod(
        -np.asarray([col_offset / col_period, row_offset / row_period], dtype=np.float64),
        1.0,
    )
    return period, phase


def _validate_scanning_orientation(scanning_orientation: str) -> None:
    if not isinstance(scanning_orientation, str) or len(scanning_orientation) < 4:
        raise ValueError(
            "scanning_orientation must use the xrecon convention, e.g. 'X+Y-'"
        )
    if scanning_orientation[0] not in "XY" or scanning_orientation[2] not in "XY":
        raise ValueError(f"invalid scanning orientation: {scanning_orientation!r}")
    if scanning_orientation[0] == scanning_orientation[2]:
        raise ValueError(f"scan axes must contain X and Y once each: {scanning_orientation!r}")
    if scanning_orientation[1] not in "+-" or scanning_orientation[3] not in "+-":
        raise ValueError(f"scan directions must be + or -: {scanning_orientation!r}")


def _compute_geometry(
    scan_steps: int,
    camera_pixels: int,
    period: np.ndarray,
    oversampling: float,
    pixel_size_nm: float,
) -> IsmGeometry:
    if oversampling < 1.0:
        raise ValueError(f"oversampling must be >= 1, got {oversampling}")
    if pixel_size_nm <= 0:
        raise ValueError(f"pixel_size_nm must be positive, got {pixel_size_nm}")

    p = int(np.ceil(np.max(period * oversampling)))
    p = p + int(np.mod(p + 1, 2))  # odd, matching monalisa-xrecon
    u = int(np.floor(camera_pixels / np.mean(period)))
    if u < 1:
        raise ValueError(
            "Detected pattern period is too large for the camera frame: "
            f"period={period.tolist()}, camera_pixels={camera_pixels}"
        )
    return IsmGeometry(
        scan_steps=scan_steps,
        camera_pixels=camera_pixels,
        pixels_per_ulens=p,
        ulenses_per_axis=u,
        output_pixels=u * scan_steps,
        output_pixel_size_nm=float(pixel_size_nm * period[0] / p),
        scanning_step_px=float(scan_steps / p),
    )


def _least_square_signal_weights(pixels_per_ulens: int, sigma_px: float) -> np.ndarray:
    p = int(pixels_per_ulens)
    if not np.isfinite(sigma_px) or sigma_px <= 0:
        raise ValueError(f"PSF sigma must be finite and positive, got {sigma_px}")

    x = (np.arange(p, dtype=np.float64) + 1.0) - 0.5 - p / 2.0
    X, Y = np.meshgrid(x, x)
    scale = np.floor(p / 2.0)
    X /= scale
    Y /= scale

    gaussian = np.exp(-(X**2 + Y**2) / (2.0 * sigma_px**2))
    constant = np.ones((p, p), dtype=np.float64)
    model = np.stack((gaussian, constant), axis=0).reshape(2, p * p)
    pinv = np.linalg.pinv(model).T.reshape(2, p, p)
    return np.asarray(pinv[0], dtype=WORKING_FLOAT_DTYPE)


def _interpolation_indices(source_coords: np.ndarray, size: int):
    clipped = np.clip(np.asarray(source_coords, dtype=np.float64), 0.0, size - 1.0)
    lower = np.clip(np.floor(clipped).astype(INDEX_DTYPE), 0, size - 2)
    upper = lower + 1
    upper_weight = (clipped - lower).astype(WORKING_FLOAT_DTYPE)
    lower_weight = (1.0 - upper_weight).astype(WORKING_FLOAT_DTYPE)
    return lower, upper, lower_weight, upper_weight


def _source_coordinates(period: np.ndarray, phase: np.ndarray, geometry: IsmGeometry):
    p = geometry.pixels_per_ulens
    u = geometry.ulenses_per_axis
    xu = (np.arange(u * p, dtype=np.float64) + 0.5) / p + 0.5
    source_cols = (xu - phase[0]) * period[0]
    source_rows = (xu - phase[1]) * period[1]
    return source_rows, source_cols


def _reshape_and_fix_scanning(raw_data, scanning_orientation: str, cp):
    _validate_scanning_orientation(scanning_orientation)
    m, n = validate_raw_stack(raw_data)
    data = cp.asarray(raw_data).reshape(m, m, n, n)

    if len(scanning_orientation) > 4 and scanning_orientation[4] == "b":
        data = data.copy()
        even_indexes = cp.arange(1, m, 2)
        data[even_indexes, :] = cp.flip(data[even_indexes, :], axis=1)
    if scanning_orientation[1] == "-":
        data = cp.flip(data, axis=1)
    if scanning_orientation[3] == "-":
        data = cp.flip(data, axis=0)
    if scanning_orientation[0:3:2] == "YX":
        data = cp.transpose(data, axes=(1, 0, 2, 3))
    return data


def _center_ulenses_and_reassign(
    data,
    period: np.ndarray,
    phase: np.ndarray,
    geometry: IsmGeometry,
    cp,
    *,
    frame_batch_size: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
):
    """Batched separable linear interpolation entirely on the GPU."""
    data = cp.asarray(data, dtype=cp.float32)
    m = geometry.scan_steps
    n = geometry.camera_pixels
    p = geometry.pixels_per_ulens
    u = geometry.ulenses_per_axis

    source_rows, source_cols = _source_coordinates(period, phase, geometry)
    row_lo, row_hi, row_w0, row_w1 = _interpolation_indices(source_rows, n)
    col_lo, col_hi, col_w0, col_w1 = _interpolation_indices(source_cols, n)

    row_lo, row_hi = cp.asarray(row_lo), cp.asarray(row_hi)
    row_w0, row_w1 = cp.asarray(row_w0), cp.asarray(row_w1)
    col_lo, col_hi = cp.asarray(col_lo), cp.asarray(col_hi)
    col_w0, col_w1 = cp.asarray(col_w0), cp.asarray(col_w1)

    frames = data.reshape(m * m, n, n)
    frame_grid = cp.empty((m * m, p, p, u, u), dtype=cp.float32)
    total_frames = m * m
    if frame_batch_size is None or int(frame_batch_size) <= 0:
        frame_batch_size = total_frames
    frame_batch_size = min(int(frame_batch_size), total_frames)

    for start in range(0, total_frames, frame_batch_size):
        if check_cancelled is not None:
            check_cancelled()
        stop = min(start + frame_batch_size, total_frames)
        batch = frames[start:stop]

        tmp = (
            batch[:, row_lo, :] * row_w0[None, :, None]
            + batch[:, row_hi, :] * row_w1[None, :, None]
        )
        centered = (
            tmp[:, :, col_lo] * col_w0[None, None, :]
            + tmp[:, :, col_hi] * col_w1[None, None, :]
        )
        frame_grid[start:stop] = centered.reshape(
            stop - start, u, p, u, p
        ).transpose(0, 2, 4, 1, 3)

    frame_grid = frame_grid.reshape(m, m, p, p, u, u)
    frame_grid = cp.flip(frame_grid, axis=(0, 1))
    return cp.transpose(frame_grid, (2, 3, 4, 0, 5, 1)).reshape(
        p, p, u * m, u * m
    )


def reconstruct_ism_gpu(
    raw_data,
    *,
    pattern_period: Sequence[float],
    pattern_phase: Sequence[float],
    scanning_orientation: str = "X+Y-",
    psf_fwhm_nm: float = 200.0,
    pixel_size_nm: float = 100.0,
    oversampling: float = 2.0,
    ism_shift: float = 0.5,
    remove_mean_of_patch: bool = True,
    frame_batch_size: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
):
    """Run the extracted ISM reassignment entirely on an NVIDIA GPU."""
    cp = _import_cupy()
    if cp.cuda.runtime.getDeviceCount() < 1:
        raise RuntimeError("CuPy is installed, but no CUDA device is visible")

    m, n = validate_raw_stack(raw_data)
    period = np.asarray(pattern_period, dtype=np.float64)
    phase = np.asarray(pattern_phase, dtype=np.float64)
    if period.shape != (2,) or phase.shape != (2,):
        raise ValueError("pattern_period and pattern_phase must each contain x and y")
    if not np.isfinite(period).all() or np.any(period <= 0) or not np.isfinite(phase).all():
        raise ValueError("pattern period/phase must be finite and periods must be positive")
    if psf_fwhm_nm <= 0:
        raise ValueError("PSF FWHM must be positive")

    geometry = _compute_geometry(m, n, period, float(oversampling), float(pixel_size_nm))
    data = _reshape_and_fix_scanning(raw_data, scanning_orientation, cp)
    reassigned = _center_ulenses_and_reassign(
        data,
        period,
        phase,
        geometry,
        cp,
        frame_batch_size=frame_batch_size,
        check_cancelled=check_cancelled,
    )

    if check_cancelled is not None:
        check_cancelled()
    if remove_mean_of_patch:
        reassigned -= cp.mean(reassigned, axis=(0, 1))

    sigma_px = float(psf_fwhm_nm) / (2.355 * geometry.output_pixel_size_nm)
    weights = cp.asarray(
        _least_square_signal_weights(geometry.pixels_per_ulens, sigma_px),
        dtype=cp.float32,
    )

    if float(ism_shift) == 0.0:
        image = cp.einsum("ij,ijnm->nm", weights, reassigned).astype(
            cp.float32, copy=False
        )
    else:
        p = geometry.pixels_per_ulens
        out_n = geometry.output_pixels
        delta = cp.asarray(
            (
                ((np.arange(p) + 1) - 0.5 - p / 2)
                * float(ism_shift)
                * geometry.scanning_step_px
            ).astype(WORKING_FLOAT_DTYPE)
        )
        freq_y = cp.fft.fftfreq(out_n).astype(cp.float32)
        freq_x = cp.fft.rfftfreq(out_n).astype(cp.float32)
        phase_y = cp.exp(
            (-2j * cp.pi) * delta[:, None] * freq_y[None, :]
        ).astype(cp.complex64)
        phase_x = cp.exp(
            (-2j * cp.pi) * delta[:, None] * freq_x[None, :]
        ).astype(cp.complex64)

        # Fused Fourier shift + Gaussian fit.  Keeping the spectrum in-place
        # avoids a full additional P*P*N*N complex phase/product volume.
        spectrum = cp.fft.rfft2(reassigned, axes=(-2, -1))
        spectrum *= phase_y[:, None, :, None]
        spectrum *= phase_x[None, :, None, :]
        spectrum *= weights[:, :, None, None]
        image_spectrum = cp.sum(spectrum, axis=(0, 1))
        image = cp.fft.irfft2(
            image_spectrum, s=(out_n, out_n), axes=(-2, -1)
        ).astype(cp.float32, copy=False)

    if check_cancelled is not None:
        check_cancelled()
    cp.cuda.get_current_stream().synchronize()
    return cp.asnumpy(image), geometry


__all__ = [
    "IsmGeometry",
    "gpu_available",
    "patternfinder_to_xrecon",
    "reconstruct_ism_gpu",
    "validate_raw_stack",
]

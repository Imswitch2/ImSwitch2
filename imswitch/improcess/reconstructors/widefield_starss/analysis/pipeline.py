"""Array-level WidefieldSTARSS analysis pipeline for ImProcess.

This module intentionally accepts already-loaded NumPy arrays. File discovery,
dialogs, and ImProcess ``DataObj`` handling live in the reconstructor layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from .anisotropy import build_anisotropy_maps, build_anisotropy_maps_split_detection
from .containers import AnisotropyMaps, PolarizationStats
from .polarization import compute_polarization_stats, compute_simple_intensity_stats
from .regions import (
    analyze_regions,
    analyze_regions_split_detection,
    analyze_split_detection_line_psf,
)
from .segmentation import (
    build_mask,
    build_psf_mask,
    make_simple_mask,
    segment_line_psf,
)


FrameConvention = Literal["alternating", "block"]
# ``generic_otsu`` is accepted for backward compatibility with older presets.
# It is normalized to ``otsu`` at execution time.
SegmentationMode = Literal["otsu", "none", "psf_peaks", "line_psf", "generic_otsu"]
VISIBLE_SEGMENTATION_MODES = ("none", "otsu", "psf_peaks", "line_psf")
AnisotropyMode = Literal["stokes", "direct_0_90"]


@dataclass(frozen=True)
class WidefieldStarssParams:
    """Parameters for one H/V WidefieldSTARSS analysis."""

    convention: FrameConvention = "alternating"
    start_frame: int = 0
    n_dark: int = 0
    n_off: int = 0
    sum_stacks: bool = False
    roi: tuple[int, int, int, int] | None = None
    split_detection: bool = False
    split_y: int | None = None
    anisotropy_mode: AnisotropyMode = "stokes"
    segmentation_mode: SegmentationMode = "otsu"
    segmentation_sigma: float = 2.0
    min_size: int = 200
    hole_size: int = 200
    threshold_scale: float = 1.0
    psf_sigma: float = 2.0
    psf_min_distance: int = 5
    psf_threshold_rel: float = 0.1
    psf_radius: int = 3
    smooth_sigma: float = 2.0
    intensity_threshold: float | None = None


@dataclass
class WidefieldStarssAnalysis:
    """Complete output of one WidefieldSTARSS analysis."""

    regions: pd.DataFrame
    mask: np.ndarray
    base_image: np.ndarray
    anis_maps: AnisotropyMaps
    stats_h: PolarizationStats | None = None
    stats_v: PolarizationStats | None = None
    params: WidefieldStarssParams | None = None


def _canonical_segmentation_mode(
    mode: SegmentationMode,
) -> Literal["otsu", "none", "psf_peaks", "line_psf"]:
    if mode == "generic_otsu":
        return "otsu"
    return mode


def prepare_signal_background(
    stack: np.ndarray,
    params: WidefieldStarssParams,
) -> tuple[np.ndarray, np.ndarray]:
    """Split a raw stack into signal and background stacks."""
    arr = np.asarray(stack)
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    if arr.ndim != 3:
        raise ValueError(f"Expected a 2D frame or 3D stack, got shape {arr.shape}")

    if params.roi is not None:
        r0, r1, c0, c1 = params.roi
        arr = arr[:, r0:r1, c0:c1]

    if params.convention == "alternating":
        work = arr[int(params.start_frame):]
        if work.shape[0] < 2:
            raise ValueError("Alternating convention needs at least one signal/background pair")
        signal = work[0::2]
        background = work[1::2]
        n_pairs = min(signal.shape[0], background.shape[0])
        signal = signal[:n_pairs]
        background = background[:n_pairs]
    elif params.convention == "block":
        n_dark = int(params.n_dark)
        n_off = int(params.n_off)
        if n_dark < 0 or n_off < 0:
            raise ValueError("n_dark and n_off must be non-negative")
        if arr.shape[0] <= n_dark + n_off:
            raise ValueError("Block convention needs frames after dark/off sections")
        signal = arr[n_dark + n_off:]
        if n_off > 0:
            off_mean = np.mean(arr[n_dark:n_dark + n_off], axis=0, keepdims=True)
        elif n_dark > 0:
            off_mean = np.mean(arr[:n_dark], axis=0, keepdims=True)
        else:
            off_mean = np.zeros((1, *arr.shape[1:]), dtype=arr.dtype)
        background = np.repeat(off_mean, signal.shape[0], axis=0)
    else:
        raise ValueError(f"Unsupported frame convention: {params.convention!r}")

    if signal.shape[0] == 0 or background.shape[0] == 0:
        raise ValueError("No signal/background frames available after splitting")

    if params.sum_stacks:
        signal = np.sum(signal, axis=0, keepdims=True)
        background = np.sum(background, axis=0, keepdims=True)

    return signal.astype(np.float32, copy=False), background.astype(np.float32, copy=False)


def _apply_segmentation_mask(anis_maps: AnisotropyMaps, mask: np.ndarray) -> None:
    """Restrict anisotropy maps to segmented regions (mask label > 0)."""
    segmented = np.asarray(mask) > 0
    if segmented.shape != anis_maps.r_raw.shape:
        raise ValueError(
            f"Segmentation mask shape {segmented.shape} does not match "
            f"anisotropy map shape {anis_maps.r_raw.shape}"
        )
    for attr in ("x_raw", "r_raw", "r_raw_se", "x_smooth", "r_smooth", "r_smooth_se"):
        values = getattr(anis_maps, attr)
        setattr(anis_maps, attr, np.where(segmented, values, np.nan))
    anis_maps.valid_mask = anis_maps.valid_mask & segmented


def analyze_widefield_starss_pair(
    stack_h: np.ndarray,
    stack_v: np.ndarray,
    params: WidefieldStarssParams | None = None,
) -> WidefieldStarssAnalysis:
    """Analyze one H/V acquisition pair."""
    params = params or WidefieldStarssParams()
    signal_h, background_h = prepare_signal_background(stack_h, params)
    signal_v, background_v = prepare_signal_background(stack_v, params)

    if params.split_detection:
        return _analyze_split_detection(signal_h, background_h, signal_v, background_v, params)
    return _analyze_standard_mosaic(signal_h, background_h, signal_v, background_v, params)


def _analyze_standard_mosaic(
    signal_h: np.ndarray,
    background_h: np.ndarray,
    signal_v: np.ndarray,
    background_v: np.ndarray,
    params: WidefieldStarssParams,
) -> WidefieldStarssAnalysis:
    stats_h = compute_polarization_stats(signal_h, background_h)
    stats_v = compute_polarization_stats(signal_v, background_v)
    segmentation_mode = _canonical_segmentation_mode(params.segmentation_mode)

    if segmentation_mode == "psf_peaks":
        mask, base_image = build_psf_mask(
            stats_h,
            stats_v=stats_v,
            sigma=params.psf_sigma,
            min_distance=params.psf_min_distance,
            threshold_rel=params.psf_threshold_rel,
            psf_radius=params.psf_radius,
        )
    else:
        mask, base_image = build_mask(
            stats_h,
            stats_v=stats_v,
            segment=segmentation_mode != "none",
            sigma=params.segmentation_sigma,
            min_size=params.min_size,
            hole_size=params.hole_size,
            threshold_scale=params.threshold_scale,
        )

    anis_maps = build_anisotropy_maps(
        stats_h,
        stats_v,
        smooth_sigma=params.smooth_sigma,
        intensity_threshold=params.intensity_threshold,
        anisotropy_mode=params.anisotropy_mode,
    )
    regions = analyze_regions(
        stats_h,
        stats_v,
        anis_maps,
        mask,
        anisotropy_mode=params.anisotropy_mode,
    )
    if segmentation_mode != "none":
        _apply_segmentation_mask(anis_maps, mask)

    return WidefieldStarssAnalysis(
        regions=regions,
        mask=mask,
        base_image=base_image,
        anis_maps=anis_maps,
        stats_h=stats_h,
        stats_v=stats_v,
        params=params,
    )


def _analyze_split_detection(
    signal_h: np.ndarray,
    background_h: np.ndarray,
    signal_v: np.ndarray,
    background_v: np.ndarray,
    params: WidefieldStarssParams,
) -> WidefieldStarssAnalysis:
    split_y = params.split_y if params.split_y is not None else signal_h.shape[1] // 2
    if split_y <= 0 or split_y >= signal_h.shape[1]:
        raise ValueError(f"Invalid split_y={split_y} for image height {signal_h.shape[1]}")

    ihh, ihh_var = compute_simple_intensity_stats(signal_h[:, :split_y], background_h[:, :split_y])
    ihv, ihv_var = compute_simple_intensity_stats(signal_h[:, split_y:], background_h[:, split_y:])
    ivh, ivh_var = compute_simple_intensity_stats(signal_v[:, :split_y], background_v[:, :split_y])
    ivv, ivv_var = compute_simple_intensity_stats(signal_v[:, split_y:], background_v[:, split_y:])

    anis_maps = build_anisotropy_maps_split_detection(
        ihh,
        ihv,
        ivh,
        ivv,
        ihh_var,
        ihv_var,
        ivh_var,
        ivv_var,
        smooth_sigma=params.smooth_sigma,
        intensity_threshold=params.intensity_threshold,
    )
    base_image = 0.25 * (ihh + ihv + ivh + ivv)
    segmentation_mode = _canonical_segmentation_mode(params.segmentation_mode)

    if segmentation_mode == "line_psf":
        upper_base = 0.5 * (ihh + ivh)
        lower_base = 0.5 * (ihv + ivv)
        upper_mask = segment_line_psf(
            upper_base,
            sigma=params.psf_sigma,
            threshold_scale=params.threshold_scale,
            min_size=params.min_size,
        )
        lower_mask = segment_line_psf(
            lower_base,
            sigma=params.psf_sigma,
            threshold_scale=params.threshold_scale,
            min_size=params.min_size,
        )
        mask = np.where(upper_mask > 0, 1, np.where(lower_mask > 0, 2, 0)).astype(np.int32)
        regions = analyze_split_detection_line_psf(
            ihh,
            ihv,
            ivh,
            ivv,
            ihh_var,
            ihv_var,
            ivh_var,
            ivv_var,
            upper_mask,
            lower_mask,
        )
    else:
        if segmentation_mode == "none":
            mask = np.ones_like(base_image, dtype=np.int32)
        else:
            mask = make_simple_mask(
                base_image,
                sigma=params.segmentation_sigma,
                min_size=params.min_size,
                hole_size=params.hole_size,
                threshold_scale=params.threshold_scale,
            )
        regions = analyze_regions_split_detection(
            ihh,
            ihv,
            ivh,
            ivv,
            ihh_var,
            ihv_var,
            ivh_var,
            ivv_var,
            anis_maps,
            mask,
        )

    if segmentation_mode != "none":
        _apply_segmentation_mask(anis_maps, mask)

    return WidefieldStarssAnalysis(
        regions=regions,
        mask=mask,
        base_image=base_image,
        anis_maps=anis_maps,
        params=params,
    )

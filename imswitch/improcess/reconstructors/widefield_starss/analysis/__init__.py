"""WidefieldSTARSS numerical analysis kernels for ImProcess."""

from .anisotropy import (
    anisotropy_from_x,
    build_anisotropy_maps,
    build_anisotropy_maps_split_detection,
    gaussian_fit_anisotropy,
)
from .containers import AnisotropyMaps, PolarizationStats
from .pipeline import (
    WidefieldStarssAnalysis,
    WidefieldStarssParams,
    analyze_widefield_starss_pair,
    prepare_signal_background,
)
from .polarization import (
    compute_polarization_stats,
    compute_simple_intensity_stats,
    split_frame_into4,
    split_stack_into4,
)
from .regions import analyze_regions, analyze_regions_split_detection
from .segmentation import build_mask, build_psf_mask, make_simple_mask, segment_line_psf

__all__ = [
    "AnisotropyMaps",
    "PolarizationStats",
    "WidefieldStarssAnalysis",
    "WidefieldStarssParams",
    "analyze_regions",
    "analyze_regions_split_detection",
    "analyze_widefield_starss_pair",
    "anisotropy_from_x",
    "build_anisotropy_maps",
    "build_anisotropy_maps_split_detection",
    "build_mask",
    "build_psf_mask",
    "compute_polarization_stats",
    "compute_simple_intensity_stats",
    "gaussian_fit_anisotropy",
    "make_simple_mask",
    "prepare_signal_background",
    "segment_line_psf",
    "split_frame_into4",
    "split_stack_into4",
]

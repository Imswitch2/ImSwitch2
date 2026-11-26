"""Shared ImSwitch parameter contract for the SLMcore-compatible localizer.

Names/defaults intentionally follow SLMcore's ``LOCALIZATION_PARAMS`` where the
concept is host-independent.  Host-specific source selectors (target geometry,
uLenses widget values, etc.) remain in their respective adapters.
"""

from __future__ import annotations

from types import MappingProxyType

LOCALIZATION_DEFAULTS = MappingProxyType({
    "registration_mode": "auto",
    "crop_threshold": 0.40,
    "crop_margin_px": 1,
    "spot_threshold_rel": 0.10,
    "spot_blur_sigma": 1.0,
    "focus_localization_method": "cog",
    "focus_search_window_px": 2,
    "spot_min_distance_fraction": 0.35,
    "period_tolerance_fraction": 0.25,
    "fft_exclude_fraction": 0.03,
    "fft_peak_count": 14,
    "lattice_candidate_search_mode": "fast",
    "matching_gate_fraction": 0.40,
    "robust_outlier_sigma": 3.5,
    "max_registration_iterations": 6,
    "min_match_fraction": 0.35,
})

# UI-neutral metadata used by both uLenses and Transform Calibration adapters.
LOCALIZATION_PARAMETER_INFO = MappingProxyType({
    "registration_mode": {
        "label": "Registration", "choices": ("auto", "finite_grid", "fft_lattice"),
        "tooltip": "Auto prefers finite row/column topology and falls back to FFT lattice search. Finite Grid forces finite topology. FFT Lattice forces periodic FFT registration.",
        "group": "Grid geometry",
    },
    "crop_threshold": {
        "label": "Crop threshold", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "decimals": 2,
        "tooltip": "Fraction of the image maximum used to find the automatic pattern crop.", "group": "Grid localization",
    },
    "crop_margin_px": {
        "label": "Crop margin (px)", "minimum": 0, "step": 1,
        "tooltip": "Extra pixels added on every side of the automatically detected crop.", "group": "Grid localization",
    },
    "spot_threshold_rel": {
        "label": "Spot threshold", "minimum": 0.0, "maximum": 1.0, "step": 0.02, "decimals": 2,
        "tooltip": "Minimum relative local-maximum intensity after smoothing and background subtraction.", "group": "Grid localization",
    },
    "spot_blur_sigma": {
        "label": "Spot smoothing (px)", "minimum": 0.0, "step": 0.1, "decimals": 2,
        "tooltip": "Gaussian smoothing sigma applied before spot detection.", "group": "Grid localization",
    },
    "focus_localization_method": {
        "label": "Spot center method", "choices": ("max", "cog"),
        "tooltip": "Subpixel center refinement after peak detection.", "group": "Spot refinement", "advanced": True,
    },
    "focus_search_window_px": {
        "label": "Center refinement window (px)", "minimum": 0, "step": 1,
        "tooltip": "Half-width of the local window used to refine each detected spot center.", "group": "Spot refinement", "advanced": True,
    },
    "spot_min_distance_fraction": {
        "label": "Minimum spot distance / period", "minimum": 0.05, "maximum": 1.0, "step": 0.05, "decimals": 2,
        "tooltip": "Period-relative lower bound used when suppressing duplicate nearby detections.", "group": "Spot refinement", "advanced": True,
    },
    "period_tolerance_fraction": {
        "label": "FFT period tolerance", "minimum": 0.0, "maximum": 2.0, "step": 0.05, "decimals": 2,
        "tooltip": "Relative tolerance used to retain period-guided FFT candidates; it does not freeze the final measured period.", "group": "Registration", "advanced": True,
    },
    "fft_exclude_fraction": {
        "label": "FFT DC exclusion fraction", "minimum": 0.0, "maximum": 0.49, "step": 0.01, "decimals": 2,
        "tooltip": "Radius around zero frequency ignored during FFT peak search, as a fraction of the smaller crop dimension.", "group": "Registration", "advanced": True,
    },
    "fft_peak_count": {
        "label": "FFT peak candidates", "minimum": 2, "maximum": 64, "step": 1,
        "tooltip": "Maximum number of strong reciprocal-space peaks retained for FFT lattice candidates.", "group": "Registration", "advanced": True,
    },
    "lattice_candidate_search_mode": {
        "label": "FFT candidate breadth", "choices": ("fast", "full"),
        "tooltip": "Fast tests the strongest plausible FFT geometries; Full retains the broader search.", "group": "Registration", "advanced": True,
    },
    "matching_gate_fraction": {
        "label": "Matching radius / period", "minimum": 0.05, "maximum": 2.0, "step": 0.05, "decimals": 2,
        "tooltip": "Maximum target-to-detection matching distance as a fraction of the smaller fitted period.", "group": "Registration", "advanced": True,
    },
    "robust_outlier_sigma": {
        "label": "Outlier rejection sigma", "minimum": 0.5, "maximum": 20.0, "step": 0.5, "decimals": 1,
        "tooltip": "Robust residual threshold used while refining the affine fit.", "group": "Registration", "advanced": True,
    },
    "max_registration_iterations": {
        "label": "Registration iterations", "minimum": 1, "maximum": 50, "step": 1,
        "tooltip": "Maximum match/refit iterations used by affine registration.", "group": "Registration", "advanced": True,
    },
    "min_match_fraction": {
        "label": "Minimum matched fraction", "minimum": 0.05, "maximum": 1.0, "step": 0.05, "decimals": 2,
        "tooltip": "Smallest fraction of target sites required for a numerical fit to remain viable.", "group": "Registration", "advanced": True,
    },
})

__all__ = ["LOCALIZATION_DEFAULTS", "LOCALIZATION_PARAMETER_INFO"]

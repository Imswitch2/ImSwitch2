"""Parameter definitions for reusable target-aware image localization."""

from __future__ import annotations

from types import MappingProxyType

from ...engine.parameters import EditorKind,ParamDisplayLevel,ParamSpec


LOCALIZATION_PARAMS = MappingProxyType({
    # ------------------------------------------------------------------
    # Pattern geometry / registration strategy
    # ------------------------------------------------------------------
    "pattern_geometry_type":ParamSpec(
        "lattice",str,choices=("lattice",),
        editor=EditorKind.COMBO_BOX,
        label="Pattern geometry",
        tooltip=(
            "Geometry family used to interpret the localized pattern. More "
            "geometry types can be added without changing the localization "
            "workflow."
        ),
    ),
    "registration_mode":ParamSpec(
        "auto",str,choices=("auto","finite_grid","fft_lattice"),
        editor=EditorKind.COMBO_BOX,
        label="Registration",
        tooltip=(
            "Auto: use finite row/column topology first when the lattice size "
            "is known, then fall back to FFT lattice search if needed. Finite "
            "Grid: force finite-target topology only. FFT Lattice: force the "
            "periodic FFT/Bravais-lattice registration path."
        ),
    ),
    "stagger_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Stagger source",
        tooltip=(
            "Target: use structural stagger exposed by the resolved target; "
            "Manual: impose the value below; Auto: infer stagger from the image."
        ),
    ),
    "manual_stagger":ParamSpec(
        0.0,float,min_value=-1.0,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Stagger",
        tooltip="Signed odd-row X shift as a fraction of the X lattice period.",
    ),
    "period_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Period source",
        tooltip=(
            "Target: derive detector-pixel periods from target geometry and "
            "calibration when possible; Manual: use the values below; Auto: "
            "infer geometry from the image. Supplied periods guide spot detection "
            "and FFT candidate selection; the measured affine periods may refine."
        ),
    ),
    "expected_period_x_px":ParamSpec(
        9.2,float,min_value=1e-9,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Expected period X (camera px)",
        tooltip="Expected camera-space length of the logical X lattice period.",
    ),
    "expected_period_y_px":ParamSpec(
        9.2,float,min_value=1e-9,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Expected period Y (camera px)",
        tooltip="Expected camera-space length of the logical Y lattice period.",
    ),
    "lattice_size_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Lattice size source",
        tooltip=(
            "Target: use X/Y point counts exposed by the resolved target; "
            "Manual: impose the counts below; Auto: infer row/column counts "
            "from the detected finite lattice."
        ),
    ),
    "manual_lattice_count_x":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        label="Lattice points X",
        tooltip="Number of target sites along the logical X lattice axis.",
    ),
    "manual_lattice_count_y":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        label="Lattice points Y",
        tooltip="Number of target sites along the logical Y lattice axis.",
    ),

    # ------------------------------------------------------------------
    # Frequently tuned spot/crop controls
    # ------------------------------------------------------------------
    "crop_threshold":ParamSpec(
        0.4,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Crop threshold",
        tooltip=(
            "Fraction of the image maximum used to find the automatic pattern "
            "crop. Increase to restrict the crop to brighter features; decrease "
            "to include dimmer outer spots."
        ),
    ),
    "crop_margin_px":ParamSpec(
        1,int,min_value=0,step=1,editor=EditorKind.SPIN_BOX,
        label="Crop margin (px)",
        tooltip=(
            "Extra pixels added on every side of the automatically detected crop. "
            "Increase if spots near the crop boundary are being clipped."
        ),
    ),
    "spot_threshold_rel":ParamSpec(
        0.10,float,min_value=0.0,max_value=1.0,step=0.02,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Spot threshold",
        tooltip=(
            "Minimum relative local-maximum intensity after smoothing and "
            "background subtraction. Increase to reject weak false detections; "
            "decrease to include dim spots."
        ),
    ),
    "spot_blur_sigma":ParamSpec(
        1.0,float,min_value=0.0,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Spot smoothing (px)",
        tooltip=(
            "Gaussian smoothing sigma applied before spot detection. Increase "
            "for noisy or broad spots; excessive smoothing can merge nearby spots."
        ),
    ),

    # ------------------------------------------------------------------
    # Spot-center refinement
    # ------------------------------------------------------------------
    "focus_localization_method":ParamSpec(
        "cog",str,choices=("max","cog","gaussian"),editor=EditorKind.COMBO_BOX,
        label="Spot center method",
        tooltip=(
            "Center refinement after peak detection. CoG uses a local "
            "center of gravity; Gaussian fits an elliptical 2D Gaussian plus "
            "background; Max uses the brightest pixel in the refinement window."
        ),
    ),
    "focus_search_window_px":ParamSpec(
        2,int,min_value=0,step=1,editor=EditorKind.SPIN_BOX,
        label="Center refinement window (px)",
        tooltip=(
            "Half-width of the local window used to refine each detected spot "
            "center. Zero disables local center refinement. Gaussian fitting "
            "falls back to CoG if the local fit is not reliable."
        ),
    ),
    "spot_min_distance_fraction":ParamSpec(
        0.35,float,min_value=0.05,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Minimum spot distance / period",
        tooltip=(
            "Period-relative lower bound considered when suppressing duplicate "
            "nearby peak detections. The detector also limits this using the "
            "center-refinement window, so this value may not control large-period lattices."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),

    # ------------------------------------------------------------------
    # Advanced registration / FFT controls
    # ------------------------------------------------------------------
    "period_tolerance_fraction":ParamSpec(
        0.25,float,min_value=0.0,max_value=2.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="FFT period tolerance",
        tooltip=(
            "Relative tolerance used to retain period-guided FFT lattice "
            "candidates. It does not freeze the final measured affine period. "
            "A value of zero means no numerical tolerance in the guided FFT filter."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "fft_exclude_fraction":ParamSpec(
        0.03,float,min_value=0.0,max_value=0.49,step=0.01,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="FFT DC exclusion fraction",
        tooltip=(
            "Radius around the zero-frequency FFT component ignored while "
            "searching for lattice peaks, as a fraction of the smaller cropped "
            "image dimension. Increase if broad background dominates the FFT; "
            "too large a value can remove peaks from large-period lattices."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "fft_peak_count":ParamSpec(
        14,int,min_value=2,max_value=64,step=1,
        editor=EditorKind.SPIN_BOX,
        label="FFT peak candidates",
        tooltip=(
            "Maximum number of strong reciprocal-space peaks retained when "
            "constructing FFT lattice-basis candidates."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "lattice_candidate_search_mode":ParamSpec(
        "fast",str,choices=("fast","full"),
        editor=EditorKind.COMBO_BOX,
        label="FFT candidate breadth",
        tooltip=(
            "Fast ranks reciprocal-lattice candidates and tests only the strongest "
            "plausible geometries. Full retains the broader legacy FFT search. "
            "This setting is used only by FFT registration or Auto fallback."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "matching_gate_fraction":ParamSpec(
        0.40,float,min_value=0.05,max_value=2.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Matching radius / period",
        tooltip=(
            "Maximum target-to-detection matching distance, expressed as a "
            "fraction of the smaller fitted lattice period. Larger values tolerate "
            "poorer initial alignment but increase the risk of incorrect matches."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "robust_outlier_sigma":ParamSpec(
        3.5,float,min_value=0.5,max_value=20.0,step=0.5,decimals=1,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Outlier rejection sigma",
        tooltip=(
            "Robust residual threshold used while refining the affine fit. Larger "
            "values reject fewer tentative matches."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "max_registration_iterations":ParamSpec(
        6,int,min_value=1,max_value=50,step=1,
        editor=EditorKind.SPIN_BOX,
        label="Registration iterations",
        tooltip="Maximum match/refit iterations used by affine registration.",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "min_match_fraction":ParamSpec(
        0.35,float,min_value=0.05,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Minimum matched fraction",
        tooltip=(
            "Smallest fraction of target sites required for a numerical fit to "
            "remain viable. Lower values permit partial localization but can make "
            "incorrect registrations easier to accept."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
})


def normalize_localization_parameter_input(values):
    """Normalize legacy localization parameter names before validation.

    ``dilation_kernel_size`` historically controlled only automatic-crop extent.
    The UI now exposes the actual per-side margin directly as ``crop_margin_px``.
    Preserve old saved parameter mappings by converting the former kernel size
    to its equivalent lower-side margin.
    """
    normalized = dict(values or {})
    if "dilation_kernel_size" in normalized:
        legacy = int(normalized.pop("dilation_kernel_size"))
        normalized.setdefault("crop_margin_px",max(0,legacy//2))
    return normalized


__all__ = ["LOCALIZATION_PARAMS","normalize_localization_parameter_input"]

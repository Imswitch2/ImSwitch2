"""Parameter definitions for reusable target-aware image localization."""

from __future__ import annotations

from types import MappingProxyType

from ...engine.parameters import EditorKind,ParamDisplayLevel,ParamSpec


# These are numerical localization choices only. Workflow policy such as
# acquiring/loading images or reusing an accepted localization belongs to the
# host workflow, not to the localization algorithm.
LOCALIZATION_PARAMS = MappingProxyType({
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
    "stagger_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Stagger source",
        tooltip=(
            "target: use structural stagger exposed by the resolved target; "
            "manual: impose the value below; auto: infer stagger from the image."
        ),
    ),
    "manual_stagger":ParamSpec(
        0.0,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Stagger",
        tooltip="Odd-row X shift as a fraction of the X lattice period.",
    ),
    "period_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Period source",
        tooltip=(
            "target: derive detector-pixel period lengths from target geometry "
            "and calibration when possible; manual: use the values below; auto: "
            "infer period from the image. A supplied period guides candidate "
            "selection and is refined by the final affine fit."
        ),
    ),
    "expected_period_x_px":ParamSpec(
        9.2,float,min_value=1e-9,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Expected period X (camera px)",
        tooltip="Strong search prior for the first lattice-vector length.",
    ),
    "expected_period_y_px":ParamSpec(
        9.2,float,min_value=1e-9,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Expected period Y (camera px)",
        tooltip="Strong search prior for the second lattice-vector length.",
    ),
    "lattice_size_prior_mode":ParamSpec(
        "target",str,choices=("target","manual","auto"),
        editor=EditorKind.COMBO_BOX,
        label="Lattice size source",
        tooltip=(
            "target: use X/Y point counts exposed by the resolved target; "
            "manual: impose the counts below; auto: infer row/column counts "
            "from the detected finite lattice."
        ),
    ),
    "manual_lattice_count_x":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        label="Lattice points X",
    ),
    "manual_lattice_count_y":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        label="Lattice points Y",
    ),
    "focus_localization_method":ParamSpec(
        "cog",str,choices=("max","cog"),editor=EditorKind.COMBO_BOX,
        label="Focus localization",
    ),
    "focus_search_window_px":ParamSpec(
        2,int,min_value=0,step=1,editor=EditorKind.SPIN_BOX,
        label="Focus search window (px)",
    ),
    "period_tolerance_fraction":ParamSpec(
        0.25,float,min_value=0.0,max_value=2.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Period tolerance",
        tooltip=(
            "Relative period window used by the guided FFT search. The final "
            "affine fit is free to refine the supplied period."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "crop_threshold":ParamSpec(
        0.4,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Crop threshold",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "dilation_kernel_size":ParamSpec(
        3,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        label="Dilation kernel size",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "spot_blur_sigma":ParamSpec(
        1.0,float,min_value=0.0,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Spot blur sigma",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "spot_threshold_rel":ParamSpec(
        0.10,float,min_value=0.0,max_value=1.0,step=0.02,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Spot threshold",
        tooltip="Relative local-maximum threshold after background suppression.",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "spot_min_distance_fraction":ParamSpec(
        0.35,float,min_value=0.05,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Minimum spot distance / period",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "fft_exclude_fraction":ParamSpec(
        0.03,float,min_value=0.0,max_value=0.49,step=0.01,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="FFT DC exclusion fraction",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "fft_peak_count":ParamSpec(
        14,int,min_value=2,max_value=64,step=1,
        editor=EditorKind.SPIN_BOX,
        label="FFT peak candidates",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "lattice_candidate_search_mode":ParamSpec(
        "fast",str,choices=("fast","full"),
        editor=EditorKind.COMBO_BOX,
        label="Lattice candidate search",
        tooltip=(
            "fast: rank reciprocal-lattice candidates and correlate only the "
            "strongest plausible geometries; full: retain the broader search."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "matching_gate_fraction":ParamSpec(
        0.40,float,min_value=0.05,max_value=2.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Matching gate / period",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "robust_outlier_sigma":ParamSpec(
        3.5,float,min_value=0.5,max_value=20.0,step=0.5,decimals=1,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Outlier rejection sigma",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "max_registration_iterations":ParamSpec(
        6,int,min_value=1,max_value=50,step=1,
        editor=EditorKind.SPIN_BOX,
        label="Registration iterations",
        display_level=ParamDisplayLevel.ADVANCED,
    ),
    "min_match_fraction":ParamSpec(
        0.35,float,min_value=0.05,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        label="Minimum matched fraction",
        tooltip=(
            "Localization may return a partial lattice above this threshold. "
            "Feedback application can impose a stricter completeness rule."
        ),
        display_level=ParamDisplayLevel.ADVANCED,
    ),
})

__all__ = ["LOCALIZATION_PARAMS"]

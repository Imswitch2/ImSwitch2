"""Parameter defaults and presets for the unified Gaussian MoNaLISA reconstructor."""

from __future__ import annotations

from copy import deepcopy

from .localization_settings import DEFAULT_LOCALIZATION_SETTINGS
from .placement import COMMENSURATE_TOLERANCE
from .spot_settings import DEFAULT_SPOT_FWHM_NM

PRESET_FAST = "Fast"
PRESET_FULL = "Full"
PRESET_LEGACY = "Legacy DLL"
PRESET_CUSTOM = "Custom"
PRESETS = (PRESET_FAST, PRESET_FULL, PRESET_LEGACY, PRESET_CUSTOM)

LOCALIZATION_AUTO = "Auto"
LOCALIZATION_LOCKED = "Locked"
LOCALIZATION_MANUAL = "Manual"

LOCALIZATION_REFINEMENT_FAST = "fast"
LOCALIZATION_REFINEMENT_FULL = "full"

ORIENTATION_SETUP = "Setup default"
ORIENTATION_RECORDED = "Recorded"
ORIENTATION_AUTO = "Auto"
ORIENTATION_LOCKED = "Locked"
ORIENTATION_MANUAL = "Manual"

SPOT_MANUAL = "Manual/default"
SPOT_AUTO = "Auto"

# Gaussian estimators. The exact string values are persisted in results/jobs.
ESTIMATOR_LOCAL = "local"
ESTIMATOR_JOINT = "joint"
ESTIMATORS = (ESTIMATOR_LOCAL, ESTIMATOR_JOINT)
FOOTPRINT_SQUARE = "square"
FOOTPRINT_CIRCLE = "circle"
FOOTPRINT_SHAPES = (FOOTPRINT_SQUARE, FOOTPRINT_CIRCLE)
FOOTPRINT_FROM_LATTICE = "from_lattice"
FOOTPRINT_FIXED = "fixed"
FOOTPRINT_MODES = (FOOTPRINT_FROM_LATTICE, FOOTPRINT_FIXED)

PLACEMENT_AUTO = "Auto"
PLACEMENT_DIRECT = "Direct"
PLACEMENT_INTERPOLATE = "Interpolate"

DEFAULT_PARAMS = {
    "preset": PRESET_FAST,
    "device": "GPU",
    "pixel_size_nm": 77.0,
    "localization_mode": LOCALIZATION_AUTO,
    # Transient resolved Auto localization.  It is deliberately part of the
    # parameter contract rather than hidden reconstructor state so preview,
    # reconstruction and multi-data reuse all consume exactly the same value.
    # The session registry mirrors this transient value without making it Locked.
    "auto_localization": {},
    # A Locked selection is resolved to an immutable payload by the parameter
    # widget before a job starts.  The id/name are provenance; reconstruction
    # consumes the payload so deleting/renaming the session entry cannot change
    # an already queued job or an old result.
    "locked_localization_id": "",
    "locked_localization_name": "",
    "locked_localization": {},
    # Presets select how expensive Auto localization/refinement may be.  This
    # remains hidden in the fixed widget: Custom keeps whichever preset it was
    # derived from.
    "localization_refinement": LOCALIZATION_REFINEMENT_FAST,
    "manual_offset_x_px": 10.4,
    "manual_offset_y_px": 9.89,
    "manual_period_x_px": 11.05,
    "manual_period_y_px": 11.05,
    "stagger_axis": "None",
    "stagger": 0.0,
    "localization_settings": DEFAULT_LOCALIZATION_SETTINGS.to_dict(),
    "orientation_mode": ORIENTATION_AUTO,
    "orientation": "+x+y",
    "locked_orientation": "",
    "spot_width_mode": SPOT_MANUAL,
    "spot_width_source": "Application default",
    "spot_fwhm": DEFAULT_SPOT_FWHM_NM,
    "spot_fwhm_unit": "nm",
    "background": "constant",
    "estimator": ESTIMATOR_LOCAL,
    "local_footprint_shape": FOOTPRINT_SQUARE,
    "local_footprint_mode": FOOTPRINT_FROM_LATTICE,
    "local_footprint_size": 9,
    "local_sampling": "camera_pixels",
    "legacy_bleaching_correction": False,
    "legacy_bg_modelling": "Constant",
    "legacy_bg_gaussian_size_nm": 1000.0,
    "intensity_correction": False,
    "cell_offsets": False,
    "reassignment": "off",
    "shift_factor": 0.0,
    "reassignment_asymmetric": False,
    "shift_factor_x": 0.0,
    "shift_factor_y": 0.0,
    "placement_mode": PLACEMENT_AUTO,
    "commensurate_tolerance": COMMENSURATE_TOLERANCE,
    "variance_weighting": False,
    "sharpen": False,
    "sharpen_sigma_px": 1.5,
    "sharpen_regularization": 0.1,
    "pinhole_stack": False,
    # Kept as expert overrides for recordings whose layout/attrs do not state
    # their scan geometry. Zero means "use the recording".
    "scan_steps_fast": 0,
    "scan_steps_slow": 0,
    "scan_step_nm": 0.0,
}

PRESET_VALUES = {
    PRESET_FAST: {
        "device": "GPU",
        "localization_refinement": LOCALIZATION_REFINEMENT_FAST,
        "spot_width_mode": SPOT_MANUAL,
    "spot_width_source": "Application default",
        "estimator": ESTIMATOR_LOCAL,
        "intensity_correction": False,
        "cell_offsets": False,
        "reassignment": "off",
        "reassignment_asymmetric": False,
        "placement_mode": PLACEMENT_AUTO,
        "variance_weighting": False,
        "sharpen": False,
    },
    PRESET_FULL: {
        "device": "GPU",
        "localization_refinement": LOCALIZATION_REFINEMENT_FULL,
        "spot_width_mode": SPOT_AUTO,
        "estimator": ESTIMATOR_JOINT,
        "intensity_correction": True,
        "cell_offsets": True,
        "reassignment": "auto",
        "reassignment_asymmetric": False,
        "placement_mode": PLACEMENT_AUTO,
        "variance_weighting": False,
        "sharpen": False,
    },
    PRESET_LEGACY: {
        # The classic SignalExtractor has its own background / bleaching
        # controls, but still shares localization, FWHM and the compute-device
        # setting with the unified widget.  Keep unsupported Gaussian stages
        # inert when entering Legacy DLL mode.
        "device": "CPU",
        "localization_refinement": LOCALIZATION_REFINEMENT_FAST,
        "spot_width_mode": SPOT_MANUAL,
    "spot_width_source": "Application default",
        "estimator": ESTIMATOR_JOINT,
        "intensity_correction": False,
        "cell_offsets": False,
        "reassignment": "off",
        "reassignment_asymmetric": False,
        "placement_mode": PLACEMENT_AUTO,
        "variance_weighting": False,
        "sharpen": False,
    },
}

PRESET_CONTROLLED_KEYS = frozenset(
    key for values in PRESET_VALUES.values() for key in values
)


def default_params() -> dict:
    return deepcopy(DEFAULT_PARAMS)


def apply_preset(params: dict, preset: str) -> dict:
    out = deepcopy(dict(params))
    if preset not in PRESET_VALUES:
        if preset != PRESET_CUSTOM:
            raise ValueError(f"Unknown MoNaLISA preset {preset!r}")
        out["preset"] = PRESET_CUSTOM
        return out
    out.update(deepcopy(PRESET_VALUES[preset]))
    out["preset"] = preset
    return out


__all__ = [
    "DEFAULT_PARAMS",
    "ESTIMATOR_LOCAL", "ESTIMATOR_JOINT", "ESTIMATORS",
    "FOOTPRINT_SQUARE", "FOOTPRINT_CIRCLE", "FOOTPRINT_SHAPES",
    "FOOTPRINT_FROM_LATTICE", "FOOTPRINT_FIXED", "FOOTPRINT_MODES",
    "LOCALIZATION_AUTO",
    "LOCALIZATION_LOCKED",
    "LOCALIZATION_MANUAL",
    "LOCALIZATION_REFINEMENT_FAST",
    "LOCALIZATION_REFINEMENT_FULL",
    "ORIENTATION_SETUP",
    "ORIENTATION_RECORDED",
    "ORIENTATION_AUTO",
    "ORIENTATION_LOCKED",
    "ORIENTATION_MANUAL",
    "PLACEMENT_AUTO",
    "PLACEMENT_DIRECT",
    "PLACEMENT_INTERPOLATE",
    "PRESET_CONTROLLED_KEYS",
    "PRESET_CUSTOM",
    "PRESET_FAST",
    "PRESET_FULL",
    "PRESET_LEGACY",
    "PRESET_VALUES",
    "PRESETS",
    "SPOT_AUTO",
    "SPOT_MANUAL",
    "apply_preset",
    "default_params",
]

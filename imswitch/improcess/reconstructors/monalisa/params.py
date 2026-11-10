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
# Only Gaussian processing profiles are selectable in the unified interface.
# Legacy DLL remains readable for old jobs and has a separate reconstructor.
PRESETS = (PRESET_FAST, PRESET_FULL)

FULL_OPTION_DEFAULTS = {
    "reassignment": "auto",
    "shift_factor": 0.0,
    "reassignment_asymmetric": False,
    "shift_factor_x": 0.0,
    "shift_factor_y": 0.0,
    "pinhole_stack": False,
    "sharpen": False,
}
FULL_OPTION_KEYS = tuple(FULL_OPTION_DEFAULTS)


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
JOINT_FOOTPRINT_MODES = ("auto", "fixed", "lattice")
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
    # Explicit localization refinement policy, independent of Fast/Full mode.
    # Existing saved values remain valid on replay.
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
    # Legacy per-estimator footprint keys are NOT part of the current
    # parameter contract. Old results are still interpreted by
    # footprint_from_params() before defaults are merged.
    "local_sampling": "camera_pixels",
    # One shared footprint policy for Local and Joint.
    "footprint_shape": "square",
    "footprint_mode": "from_lattice",
    "footprint_size_px": 9.0,
    "footprint_sigma_factor": 2.5,
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
    # Stored independently of the Fast execution settings. Switching away
    # from Full bypasses these stages without discarding the user's choices.
    "full_options": deepcopy(FULL_OPTION_DEFAULTS),
    # Kept as expert overrides for recordings whose layout/attrs do not state
    # their scan geometry. Zero means "use the recording".
    "scan_steps_fast": 0,
    "scan_steps_slow": 0,
    "scan_step_nm": 0.0,
}

def default_params() -> dict:
    return deepcopy(DEFAULT_PARAMS)


# Only processing stages differ between the two Gaussian modes.  In
# particular, mode selection must never rewrite localization, FWHM, fitting,
# corrections, device, placement or the numerical optimization policy.
PRESET_VALUES = {
    PRESET_FAST: {"reassignment": "off", "pinhole_stack": False, "sharpen": False},
    PRESET_FULL: deepcopy(FULL_OPTION_DEFAULTS),
}
PRESET_CONTROLLED_KEYS = frozenset(FULL_OPTION_KEYS)


def _full_options(params: dict, *, capture_effective: bool = False) -> dict:
    """Load retained Full settings; accept old records without a nested copy."""
    options = deepcopy(FULL_OPTION_DEFAULTS)
    saved = params.get("full_options")
    if isinstance(saved, dict):
        options.update({key: saved[key] for key in FULL_OPTION_KEYS if key in saved})
    elif capture_effective:
        options.update({key: params[key] for key in FULL_OPTION_KEYS if key in params})
    return options


def normalize_preset(params: dict) -> str:
    """Resolve old Custom jobs based on the processing stages they requested.

    Scientific controls from such jobs are never reset or inferred from the
    previous Fast/Full recipe.  Legacy is handled separately by its adapter.
    """
    preset = str(params.get("preset", PRESET_FAST))
    if preset == PRESET_CUSTOM:
        return (PRESET_FULL if (
            str(params.get("reassignment", "off")) != "off"
            or bool(params.get("pinhole_stack", False))
            or bool(params.get("sharpen", False))
        ) else PRESET_FAST)
    if preset not in (PRESET_FAST, PRESET_FULL, PRESET_LEGACY):
        raise ValueError(f"Unknown MoNaLISA preset {preset!r}")
    return preset


def effective_mode_params(params: dict) -> dict:
    """Make mode-specific stages explicit without changing scientific choices.

    A Fast result always has ISM/pinhole/sharpening disabled, irrespective of
    saved Full settings.  Full's explicit fields are authoritative; the nested
    settings are a remembered selection, not an override of an active job.
    """
    out = deepcopy(dict(params))
    requested = str(out.get("preset", PRESET_FAST))
    mode = normalize_preset(out)
    out["preset"] = mode
    out["full_options"] = _full_options(
        out, capture_effective=(requested in (PRESET_FULL, PRESET_CUSTOM) and
                                "full_options" not in out),
    )
    if mode == PRESET_FAST:
        out.update(PRESET_VALUES[PRESET_FAST])
    elif mode == PRESET_FULL:
        for key in FULL_OPTION_KEYS:
            out.setdefault(key, out["full_options"][key])
    return out


def apply_preset(params: dict, preset: str) -> dict:
    """Change processing profile; retain scientific and previous Full settings."""
    out = deepcopy(dict(params))
    target = normalize_preset({**out, "preset": preset})
    previous = normalize_preset(out)
    if target == PRESET_LEGACY:
        # Preserve the old API for preexisting legacy jobs, while removing this
        # choice from the unified widget and public selectable PRESETS.
        out.update({
            "device": "CPU", "localization_refinement": LOCALIZATION_REFINEMENT_FAST,
            "spot_width_mode": SPOT_MANUAL, "estimator": ESTIMATOR_JOINT,
            "intensity_correction": False, "cell_offsets": False,
            "reassignment": "off", "reassignment_asymmetric": False,
            "placement_mode": PLACEMENT_AUTO, "variance_weighting": False,
            "sharpen": False, "pinhole_stack": False,
        })
        out["preset"] = PRESET_LEGACY
        return out
    full = _full_options(
        out, capture_effective=(previous == PRESET_FULL and "full_options" not in out),
    )
    if previous == PRESET_FULL and "full_options" in out:
        # The effective fields reflect the last UI/headless edits made in Full.
        full.update({key: out[key] for key in FULL_OPTION_KEYS if key in out})
    out["full_options"] = full
    out["preset"] = target
    if target == PRESET_FULL:
        out.update(full)
    else:
        out.update(PRESET_VALUES[PRESET_FAST])
    return out


__all__ = [
    "DEFAULT_PARAMS",
    "ESTIMATOR_LOCAL", "ESTIMATOR_JOINT", "ESTIMATORS",
    "FOOTPRINT_SQUARE", "FOOTPRINT_CIRCLE", "FOOTPRINT_SHAPES",
    "FOOTPRINT_FROM_LATTICE", "FOOTPRINT_FIXED", "FOOTPRINT_MODES",
    "JOINT_FOOTPRINT_MODES",
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
    "FULL_OPTION_DEFAULTS", "FULL_OPTION_KEYS",
    "PRESET_CONTROLLED_KEYS",
    "PRESET_CUSTOM",
    "PRESET_FAST",
    "PRESET_FULL",
    "PRESET_LEGACY",
    "PRESET_VALUES",
    "PRESETS",
    "SPOT_AUTO",
    "SPOT_MANUAL",
    "apply_preset", "effective_mode_params", "normalize_preset",
    "default_params",
]

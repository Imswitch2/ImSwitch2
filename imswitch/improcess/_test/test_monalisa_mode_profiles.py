"""Mode-state and provenance regressions for shared Fast/Full Gaussian settings.

These tests intentionally do not require Qt or a camera/GPU.
"""

from copy import deepcopy

import pytest

from imswitch.improcess.reconstructors.monalisa.params import (
    FULL_OPTION_DEFAULTS, PRESET_CUSTOM, PRESET_FAST, PRESET_FULL, PRESET_LEGACY,
    PRESETS, apply_preset, default_params, effective_mode_params, normalize_preset,
)


def test_new_modes_use_existing_combo_contract_and_full_defaults():
    assert PRESETS == (PRESET_FAST, PRESET_FULL, "DLL-equivalent")
    start = default_params()
    assert start["preset"] == PRESET_FAST
    assert start["reassignment"] == "off"
    assert start["full_options"] == FULL_OPTION_DEFAULTS
    assert start["full_options"]["reassignment"] == "auto"
    assert start["full_options"]["sharpen"] is False
    full = apply_preset(start, PRESET_FULL)
    assert full["reassignment"] == "auto"
    assert full["sharpen"] is False
    assert full["spot_width_mode"] == start["spot_width_mode"]
    assert full["estimator"] == start["estimator"]


def test_switching_only_changes_optional_processing():
    params = default_params()
    params.update(
        localization_mode="Manual", orientation_mode="Manual", orientation="-x+y",
        spot_fwhm=193., spot_width_mode="Manual/default", estimator="joint",
        footprint_shape="circle", footprint_mode="fixed", footprint_size_px=10.5,
        localization_refinement="full", intensity_correction=True,
        cell_offsets=True, variance_weighting=True, placement_mode="Interpolate",
        device="CPU",
    )
    independent = set(params) - set(FULL_OPTION_DEFAULTS) - {"full_options", "preset", "gaussian_mode", "gaussian_estimator"}
    full = apply_preset(params, PRESET_FULL)
    normal = apply_preset(full, PRESET_FAST)
    restored = apply_preset(normal, PRESET_FULL)
    for key in independent:
        assert full[key] == normal[key] == restored[key] == params[key], key
    assert normal["reassignment"] == "off"
    assert restored["reassignment"] == "auto"


def test_remembers_all_full_only_choices_not_just_ism_mode():
    full = apply_preset(default_params(), PRESET_FULL)
    full.update(reassignment="fixed", shift_factor=0.42,
                reassignment_asymmetric=True, shift_factor_x=0.39,
                shift_factor_y=0.44, pinhole_stack=True, sharpen=True)
    fast = apply_preset(full, PRESET_FAST)
    assert fast["reassignment"] == "off"
    assert fast["sharpen"] is fast["pinhole_stack"] is False
    assert fast["full_options"]["reassignment"] == "fixed"
    restored = apply_preset(fast, PRESET_FULL)
    for key in FULL_OPTION_DEFAULTS:
        assert restored[key] == full[key], key
    restored["reassignment"] = "off"
    restored["sharpen"] = False
    assert restored["preset"] == PRESET_FULL  # no Custom on edits


def test_full_off_is_effectively_identical_to_fast():
    params = apply_preset(default_params(), PRESET_FULL)
    params.update(reassignment="off", pinhole_stack=False, sharpen=False)
    fast = apply_preset(params, PRESET_FAST)
    full_effective = effective_mode_params(params)
    fast_effective = effective_mode_params(fast)
    for key in default_params():
        if key in ("preset", "full_options", "gaussian_mode", "gaussian_estimator"):
            continue
        assert full_effective[key] == fast_effective[key], key


def test_effective_fast_bypasses_full_even_if_raw_fields_are_stale():
    params = default_params()
    params.update(reassignment="fixed", pinhole_stack=True, sharpen=True)
    untouched = deepcopy(params)
    effective = effective_mode_params(params)
    assert effective["reassignment"] == "off"
    assert not effective["pinhole_stack"] and not effective["sharpen"]
    assert params == untouched


def test_old_custom_maps_without_changing_scientific_settings():
    params = default_params()
    params.pop("full_options")
    params.update(preset=PRESET_CUSTOM, estimator="joint", spot_fwhm=250,
                  reassignment="fixed", shift_factor=0.47, sharpen=False)
    migrated = effective_mode_params(params)
    assert migrated["preset"] == PRESET_FULL
    assert migrated["reassignment"] == "fixed"
    assert migrated["shift_factor"] == .47
    assert migrated["full_options"]["reassignment"] == "fixed"
    assert migrated["estimator"] == "joint"
    params.update(reassignment="off")
    assert normalize_preset(params) == PRESET_FAST
    assert effective_mode_params(params)["estimator"] == "joint"


def test_unknown_mode_rejected_but_legacy_retained_for_old_jobs():
    params = default_params()
    params["preset"] = PRESET_LEGACY
    assert effective_mode_params(params)["preset"] == PRESET_LEGACY
    assert PRESET_LEGACY not in PRESETS
    with pytest.raises(ValueError, match="Unknown"):
        apply_preset(params, "mystery")


def test_sparse_full_job_gets_auto_ism_and_explicit_off_is_respected():
    assert effective_mode_params({"preset": PRESET_FULL})["reassignment"] == "auto"
    assert effective_mode_params({"preset": PRESET_FULL, "reassignment": "off"})["reassignment"] == "off"
    assert effective_mode_params({"preset": PRESET_FULL, "reassignment": "fixed", "shift_factor": .38})["full_options"]["shift_factor"] == .38

import json

from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model.configfiletools import pruneDefaultSetupInfoFields


def _canonical_slm_entry(**overrides):
    entry = {
        "managerName": "HamamatsuSLMdviManager",
        "analogChannel": None,
        "digitalLine": None,
        "wavelength": None,
        "widgetOptions": None,
        "setup": {
            "identity": {
                "key": "SLM-A",
                "serial_number": "SER123",
                "display_name": "Left SLM",
            },
            "geometry": {
                "width": 1920,
                "height": 1152,
                "pixel_size_um": 9.2,
            },
            "sections": {
                "layout": {
                    "n_sections": 2,
                    "axis": "x",
                    "mode": "even",
                    "sizes": None,
                    "key_prefix": "sec_",
                },
                "customizable": False,
            },
            "hardware": None,
        },
        "startup_preferences": {
            "startup_config": "boot.h5",
            "default_planes": {},
            "section_display_mode": "tabs",
        },
    }
    entry.update(overrides)
    return entry


def test_slms_info_resolves_geometry_from_canonical_setup():
    setup_info = SetupInfo.from_json(
        json.dumps({"slms": {"SLM-A": _canonical_slm_entry()}}),
        infer_missing=True,
    )
    slm_info = setup_info.slms["SLM-A"]

    assert slm_info.width == 1920
    assert slm_info.height == 1152
    assert slm_info.pixelSize == 9.2
    assert slm_info.nSections == 2
    assert slm_info.serial_number == "SER123"
    assert not hasattr(slm_info, "wavelength")


def test_slms_info_keeps_legacy_fallbacks_for_current_managers():
    setup_info = SetupInfo.from_json(
        json.dumps({
            "slms": {
                "Legacy": {
                    "managerName": "HamamatsuSLMusbManager",
                    "serial_number": "LEGACY-SER",
                    "width": 800,
                    "height": 600,
                    "pixelSize": 20.0,
                    "nSections": 3,
                    "correctionPatternsDir": "C:/corr",
                    "wavelengthTableFile": "table.json",
                    "wavelength": 775,
                }
            }
        }),
        infer_missing=True,
    )
    slm_info = setup_info.slms["Legacy"]

    assert slm_info.width == 800
    assert slm_info.height == 600
    assert slm_info.pixelSize == 20.0
    assert slm_info.nSections == 3
    assert slm_info.serial_number == "LEGACY-SER"
    assert slm_info.correctionPatternsDir == "C:/corr"
    assert slm_info.wavelengthTableFile == "table.json"
    assert not hasattr(slm_info, "wavelength")


def test_slms_pruning_drops_null_compat_fields_but_preserves_values():
    new_setup_info = SetupInfo.from_json(
        json.dumps({"slms": {"SLM-A": _canonical_slm_entry()}}),
        infer_missing=True,
    )
    new_slm_data = pruneDefaultSetupInfoFields(new_setup_info)["slms"]["SLM-A"]

    for stale_key in (
        "analogChannel",
        "digitalLine",
        "serial_number",
        "width",
        "height",
        "pixelSize",
        "monitorIdx",
        "correctionPatternsDir",
        "wavelengthTableFile",
        "nSections",
        "widgetOptions",
        "wavelength",
    ):
        assert stale_key not in new_slm_data
    assert new_slm_data["setup"]["geometry"]["width"] == 1920
    assert new_slm_data["startup_preferences"]["startup_config"] == "boot.h5"

    legacy_setup_info = SetupInfo.from_json(
        json.dumps({
            "slms": {
                "Legacy": {
                    "managerName": "HamamatsuSLMusbManager",
                    "serial_number": "LEGACY-SER",
                    "width": 800,
                    "height": 600,
                    "pixelSize": 20.0,
                    "nSections": 3,
                }
            }
        }),
        infer_missing=True,
    )
    legacy_slm_data = pruneDefaultSetupInfoFields(legacy_setup_info)["slms"][
        "Legacy"
    ]

    assert legacy_slm_data["serial_number"] == "LEGACY-SER"
    assert legacy_slm_data["width"] == 800
    assert legacy_slm_data["height"] == 600
    assert legacy_slm_data["pixelSize"] == 20.0
    assert legacy_slm_data["nSections"] == 3

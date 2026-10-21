"""Temporary device disabling keeps definitions without exposing them at runtime."""

import json

import pytest

from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.configeditor.disabled_devices import (
    DISABLEABLE_CATEGORIES,
    device_section,
    locate_device,
    set_device_enabled,
)
from imswitch.imcontrol.model.configeditor.io import prepare_for_save


def _camera(manager="MockCameraManager"):
    return {"managerName": manager, "managerProperties": {"cameraListIndex": 0}}


def test_disabling_moves_the_complete_definition_out_of_the_active_section():
    data = {
        "detectors": {"Camera": _camera()},
        "availableWidgets": ["View"],
    }
    original = data["detectors"]["Camera"]

    assert set_device_enabled(data, "detectors", "Camera", False) is True
    assert data["detectors"] == {}
    assert data["disabledDevices"]["detectors"]["Camera"] is original
    assert locate_device(data, "detectors", "Camera") == (original, False)

    assert set_device_enabled(data, "detectors", "Camera", True) is True
    assert data["detectors"]["Camera"] is original
    assert "disabledDevices" not in data, "the empty parking store is pruned"
    assert locate_device(data, "detectors", "Camera") == (original, True)


def test_move_never_overwrites_a_same_named_device_in_the_other_state():
    data = {
        "detectors": {"Camera": _camera("ActiveManager")},
        "disabledDevices": {
            "detectors": {"Camera": _camera("ParkedManager")},
        },
    }
    with pytest.raises(ValueError, match="destination"):
        set_device_enabled(data, "detectors", "Camera", False)


def test_only_repeated_runtime_device_sections_are_disableable():
    assert set(DISABLEABLE_CATEGORIES) == {
        "detectors", "lasers", "positioners", "rotators",
        "rs232devices", "slms", "flipMirrors",
    }
    with pytest.raises(ValueError, match="does not support"):
        set_device_enabled({"microscopeStand": {}}, "stands", "Stand", False)


def test_setupinfo_round_trip_keeps_disabled_devices_as_editor_metadata():
    raw = {
        "detectors": {"Active camera": _camera("ActiveManager")},
        "disabledDevices": {
            "detectors": {"Spare camera": _camera("SpareManager")},
            "positioners": {
                "Old Z": {
                    "managerName": "OldStageManager",
                    "axes": ["Z"],
                    "managerProperties": {"port": "COM9"},
                }
            },
        },
    }

    setup = SetupInfo.from_json(json.dumps(raw), infer_missing=True)

    assert set(setup.detectors) == {"Active camera"}
    assert "Spare camera" not in setup.detectors
    assert setup.disabledDevices["detectors"]["Spare camera"]["managerName"] == "SpareManager"
    assert setup.disabledDevices["positioners"]["Old Z"]["managerProperties"]["port"] == "COM9"

    saved = configfiletools.pruneDefaultSetupInfoFields(setup)
    assert saved["disabledDevices"] == raw["disabledDevices"]
    assert set(saved["detectors"]) == {"Active camera"}


def test_prepare_for_save_preserves_nonempty_disabled_devices_and_prunes_empty_maps():
    data = {
        "detectors": {},
        "disabledDevices": {
            "detectors": {"Camera": _camera()},
            "lasers": {},
        },
    }
    saved = prepare_for_save(data)
    assert saved["disabledDevices"] == {"detectors": {"Camera": _camera()}}

    assert "disabledDevices" not in prepare_for_save({"disabledDevices": {"detectors": {}}})


def test_device_section_read_without_create_does_not_mutate():
    data = {}
    assert device_section(data, "detectors", enabled=False) == {}
    assert data == {}

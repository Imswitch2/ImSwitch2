"""Unit tests for Config Studio device-level comparison semantics (patch 3B)."""

from imswitch.imcontrol.model.configeditor import comparison
from imswitch.imcontrol.model.configeditor.disabled_devices import DISABLED_DEVICES_KEY


def _device(value: int):
    return {"managerName": "CameraManager", "managerProperties": {"id": value}}


def test_compare_configs_classifies_content_presence_and_enabled_state():
    left = {
        "detectors": {
            "Same": _device(1),
            "Different": _device(2),
            "LeftOnly": _device(3),
        },
        DISABLED_DEVICES_KEY: {
            "detectors": {"StateChanged": _device(4)},
        },
    }
    right = {
        "detectors": {
            "Same": _device(1),
            "Different": _device(20),
            "RightOnly": _device(5),
            "StateChanged": _device(4),
        },
    }

    states = comparison.compare_configs(left, right, ["detectors"])

    assert states[("detectors", "Same")] == comparison.SAME
    assert states[("detectors", "Different")] == comparison.DIFFERENT
    assert states[("detectors", "LeftOnly")] == comparison.LEFT_ONLY
    assert states[("detectors", "RightOnly")] == comparison.RIGHT_ONLY
    # Same stored dict, but disabled on the left and enabled on the right.
    assert states[("detectors", "StateChanged")] == comparison.DIFFERENT


def test_status_for_side_turns_presence_into_only_and_missing_labels():
    assert comparison.status_for_side(comparison.LEFT_ONLY, "left") == "only_here"
    assert comparison.status_for_side(comparison.LEFT_ONLY, "right") == "missing_here"
    assert comparison.status_for_side(comparison.RIGHT_ONLY, "left") == "missing_here"
    assert comparison.status_for_side(comparison.RIGHT_ONLY, "right") == "only_here"
    assert comparison.status_for_side(comparison.SAME, "left") == "same"
    assert comparison.status_for_side(comparison.DIFFERENT, "right") == "different"

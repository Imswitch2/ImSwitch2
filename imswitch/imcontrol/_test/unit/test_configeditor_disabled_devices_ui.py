"""Small Qt checks for disabled-device presentation in Config Studio."""

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


def test_canvas_shows_disabled_device_as_a_disabled_card(qapp):
    canvas = editor.DeviceCanvas()
    canvas.load({
        "detectors": {
            "Live camera": {"managerName": "TISManager", "managerProperties": {}},
        },
        "disabledDevices": {
            "detectors": {
                "Spare camera": {"managerName": "TISManager", "managerProperties": {}},
            },
        },
    })

    assert canvas._cards[("detectors", "Live camera")].enabled is True
    spare = canvas._cards[("detectors", "Spare camera")]
    assert spare.enabled is False
    assert "DISABLED" in " ".join(
        label.text() for label in spare.findChildren(editor.QLabel)
    )


def test_property_editor_exposes_enabled_checkbox_only_for_disableable_devices(qapp):
    panel = editor.PropertyEditor()
    panel.load_device(
        "detectors",
        "Spare camera",
        {"managerName": "TISManager", "managerProperties": {}},
        setup={},
        enabled=False,
    )
    assert panel._enabled_cb.isHidden() is False
    assert panel._enabled_cb.isChecked() is False

    # Special/singleton editor categories are intentionally not part of the
    # disabledDevices mechanism.
    panel.load_device(
        "stands",
        "Stand",
        {"managerName": "MockStandManager", "managerProperties": {}},
        setup={},
        enabled=True,
    )
    assert panel._enabled_cb.isHidden() is True

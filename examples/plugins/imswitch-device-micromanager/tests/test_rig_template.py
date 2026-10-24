"""``setup_templates/hamamatsu_native_vs_bridge.json``: the rig comparison setup (plan ``micromanager-bridge-rig-test.md``).

One camera, two drivers, one of them under ``disabledDevices``. These tests need no hardware and no
adapters: they read the file, flip which camera is enabled the way the config editor does, and run the
registry's setup validation on both variants (``validate-setup`` looks only at the active sections, so
each variant is validated with its camera enabled).
"""
import copy
import json
from importlib import resources

import pytest

TEMPLATE = "setup_templates/hamamatsu_native_vs_bridge.json"
NATIVE, BRIDGED = "HamamatsuNative", "HamamatsuMM"


def _text():
    return (resources.files("imswitch_device_micromanager") / TEMPLATE).read_text(encoding="utf-8")


def _data():
    return json.loads(_text())


def _registry():
    try:
        from imswitch.imcontrol.model.plugins.registry import get_default_registry
        registry = get_default_registry()
    except Exception:  # noqa: BLE001
        return None
    return registry if registry.resolve("detector", "micromanager.camera") is not None else None


def _variant(data, enabled_name):
    """``data`` with only ``enabled_name`` enabled, flipped the way the config editor does it."""
    from imswitch.imcontrol.model.configeditor.disabled_devices import set_device_enabled

    for name in (NATIVE, BRIDGED):
        set_device_enabled(data, "detectors", name, name == enabled_name)     # False: already in that state
    return data


def test_exactly_one_of_the_two_cameras_is_enabled():
    data = _data()
    enabled = set(data["detectors"])
    disabled = set(data["disabledDevices"]["detectors"])
    assert enabled | disabled == {NATIVE, BRIDGED} and not enabled & disabled
    assert len(enabled) == 1 and len(disabled) == 1


def test_it_is_shipped_but_not_offered_by_the_manifest():
    """It needs a camera, so no contribution lists it as a template (the editor would offer it everywhere)."""
    manifest = json.loads((resources.files("imswitch_device_micromanager") / "imswitch.json").read_text(encoding="utf-8"))
    for manager in manifest["contributions"]["device_managers"]:
        assert TEMPLATE not in manager["setup_templates"]


def test_the_two_entries_are_the_same_camera_through_two_drivers():
    data = _data()
    native = data["detectors"][NATIVE]
    bridged = data["disabledDevices"]["detectors"][BRIDGED]
    assert native["managerName"] == "HamamatsuManager" and native["managerProperties"]["cameraListIndex"] == 0
    assert bridged["managerName"] == "micromanager.camera"
    props = bridged["managerProperties"]
    assert (props["rs232device"], props["label"], props["adapter"], props["device"]) == (
        "MMCore", BRIDGED, "HamamatsuHam", "HamamatsuHam_DCAM")
    assert "configFile" not in data["rs232devices"]["MMCore"]["managerProperties"]       # inline mode: no .cfg needed
    for entry in (native, bridged):
        assert entry["forAcquisition"] is True
        assert "cameraPixelSizeUm" not in entry["managerProperties"]                     # the rig's measured value, not ours
    assert "digitalLine" not in native and "analogChannel" not in native


def test_the_native_block_is_the_monalisa_green_block_without_its_digital_line():
    monalisa = json.loads((resources.files("imswitch") / "_data/user_defaults/imcontrol_setups/example_monalisa.json")
                          .read_text(encoding="utf-8"))
    assert _data()["detectors"][NATIVE]["managerProperties"]["hamamatsu"] == \
        monalisa["detectors"]["Green"]["managerProperties"]["hamamatsu"]


def test_widgets_are_what_the_comparison_tool_drives():
    assert _data()["availableWidgets"] == ["Settings", "View", "Recording", "Image"]


def test_it_loads_as_view_setup_info_in_both_variants():
    from imswitch.imcontrol.view import ViewSetupInfo

    for enable, disable in ((BRIDGED, NATIVE), (NATIVE, BRIDGED)):
        data = _variant(copy.deepcopy(_data()), enable)
        info = ViewSetupInfo.from_json(json.dumps(data), infer_missing=True)
        assert list(info.detectors) == [enable]
        assert list(info.disabledDevices["detectors"]) == [disable]
    as_shipped = ViewSetupInfo.from_json(_text(), infer_missing=True)
    assert list(as_shipped.detectors) == [NATIVE] and "MMCore" in as_shipped.rs232devices


@pytest.mark.parametrize("enable, disable", [(NATIVE, BRIDGED), (BRIDGED, NATIVE)])
def test_both_variants_validate_with_no_diagnostics_at_all(enable, disable):
    from imswitch.imcontrol.model.plugins.validation import validate_setup_data

    registry = _registry()
    if registry is None:
        pytest.skip("plugin not installed (entry point undiscoverable); run `pip install -e .`")
    data = _variant(_data(), enable)
    report = validate_setup_data(data, registry)
    # Not only errors: a typo in a managerProperties key is a schema *warning*, and the file must be clean.
    assert not report.diagnostics, report.format()

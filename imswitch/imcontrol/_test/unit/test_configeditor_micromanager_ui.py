"""Config Studio and the Micro-Manager bridge: ``.cfg`` import and the label picker.

Headless: the file dialog and the report dialog are replaced, nothing touches
hardware or a loaded Micro-Manager core (the converter and the label picker only
parse the ``.cfg`` and ask the adapter libraries for device types).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")
convert = pytest.importorskip("imswitch_device_micromanager.convert")
mm_test_adapters = pytest.importorskip("mm_test_adapters")

from PyQt5.QtWidgets import QDialog, QTextEdit

from imswitch.imcontrol.view.configeditor import editor

DEMO_CFG = str(Path(mm_test_adapters.device_adapter_path()) / "MMConfig_demo.cfg")
HUB = editor.MM_HUB_MANAGER


def _make_window(tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    (setup_dir / "start.json").write_text(
        json.dumps({"detectors": {}, "availableWidgets": ["Settings"]}), encoding="utf-8")
    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [tmp_path / "none"])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")
    window = editor.MainWindow(start_folder=str(setup_dir))
    window._data.clear()
    window._data.update({"detectors": {}, "availableWidgets": ["Settings"]})
    return window


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    return _make_window(tmp_path, monkeypatch)


@pytest.fixture
def import_demo(window, monkeypatch):
    """Run the import action on ``cfg``; returns the report texts shown."""
    reports: list[tuple[str, str]] = []
    monkeypatch.setattr(editor.MainWindow, "_show_conversion_report",
                        lambda self, title, text: reports.append((title, text)))

    def run(cfg=DEMO_CFG):
        monkeypatch.setattr(editor.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (cfg, "")))
        window._import_mm_action.trigger()
        return reports

    return run


def _demo_template() -> dict:
    """The plugin's demo setup, as the editor offers it (relative configFile)."""
    import importlib.resources

    text = (importlib.resources.files("imswitch_device_micromanager")
            / "setup_templates" / "demo_scope.json").read_text(encoding="utf-8")
    return json.loads(text)


# --------------------------------------------------------------- import action
def test_import_action_is_enabled_and_placed_in_toolbar_and_palette(window):
    assert window._import_mm_action.isEnabled() is True
    assert window._import_mm_action.text() == "Import Micro-Manager…"
    assert window._left._import_mm_btn.isEnabled() is True


def test_import_action_is_disabled_with_the_package_name_when_the_plugin_is_missing(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "_micromanager_plugin_available", lambda: False)
    window = _make_window(tmp_path, monkeypatch)
    for control in (window._import_mm_action, window._left._import_mm_btn):
        assert control.isEnabled() is False
        assert "imswitch-device-micromanager" in control.toolTip()


def test_plugin_availability_is_a_find_spec_probe(monkeypatch):
    import importlib.util

    assert editor._micromanager_plugin_available() is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert editor._micromanager_plugin_available() is False


def test_import_merges_the_demo_cfg_into_the_open_config(window, import_demo):
    reports = import_demo()
    data = window._data

    hub = data["rs232devices"]["MMCore"]
    assert hub["managerName"] == HUB
    assert hub["managerProperties"]["configFile"] == DEMO_CFG        # as chosen, absolute
    assert list(data["detectors"]) == ["Camera"]
    assert sorted(data["positioners"]) == ["XY", "Z"]
    assert len(data["lasers"]) == 2                                  # the two shutter lights
    assert sorted(data["flipMirrors"]) == [
        "Dichroic", "Emission", "Excitation", "LED", "Objective", "Path"]
    for category in ("detectors", "positioners", "lasers", "flipMirrors"):
        for device in data[category].values():
            assert device["managerName"].startswith("micromanager.")
            assert device["managerProperties"]["rs232device"] == "MMCore"
    # availableWidgets: what was there first, in order, then the new ones
    assert data["availableWidgets"][0] == "Settings"
    assert len(data["availableWidgets"]) == len(set(data["availableWidgets"])) > 1
    assert window._modified is True
    # the canvas shows what was merged
    assert window._canvas._selected is None
    assert len(reports) == 1


def test_import_shows_the_report_with_not_written_devices_and_todos(window, import_demo):
    (title, text), = import_demo()

    assert "Micro-Manager" in title
    assert "Not written" in text
    assert "DHub" in text and "Autofocus" in text
    assert "TODO" in text
    assert "Written to the setup file" in text


def test_second_import_suffixes_names_instead_of_overwriting(window, import_demo):
    reports = import_demo()
    first = copy.deepcopy(window._data)
    import_demo()
    data = window._data

    assert data["rs232devices"]["MMCore"] == first["rs232devices"]["MMCore"]
    assert set(data["rs232devices"]) == {"MMCore", "MMCore_1"}
    assert set(data["detectors"]) == {"Camera", "Camera_1"}
    assert set(data["positioners"]) == {"Z", "Z_1", "XY", "XY_1"}
    # the second set follows its own hub; the labels stay what Micro-Manager calls them
    assert data["detectors"]["Camera_1"]["managerProperties"]["rs232device"] == "MMCore_1"
    assert data["detectors"]["Camera_1"]["managerProperties"]["label"] == "Camera"
    assert data["detectors"]["Camera"] == first["detectors"]["Camera"]
    assert len(data["availableWidgets"]) == len(first["availableWidgets"])
    assert "'MMCore_1'" in reports[-1][1] and "suffixed" in reports[-1][1]


def test_import_without_adapters_writes_the_hub_and_says_so(window, import_demo, monkeypatch):
    monkeypatch.setattr(convert, "bare_core", lambda *a, **k: (_ for _ in ()).throw(
        convert.MMAdaptersNotFoundError("no adapters on this machine")))
    (title, text), = import_demo()
    data = window._data

    assert list(data["rs232devices"]) == ["MMCore"]
    assert "detectors" not in data or not data["detectors"]
    assert "could not be read" in text and "no adapters on this machine" in text
    assert "Not written" in text and "Camera" in text            # every device is reported


def test_unreadable_cfg_changes_nothing_and_says_why(window, import_demo, monkeypatch, tmp_path):
    warnings = []
    monkeypatch.setattr(editor.QMessageBox, "warning",
                        staticmethod(lambda parent, title, text, *a: warnings.append(text)))
    bad = tmp_path / "bad.cfg"
    bad.write_text("Device,OnlyTwoFields\n", encoding="utf-8")
    before = copy.deepcopy(window._data)

    reports = import_demo(str(bad))

    assert reports == []
    assert window._data == before
    assert len(warnings) == 1 and "bad.cfg" in warnings[0]


def test_cancelled_file_dialog_changes_nothing(window, import_demo):
    before = copy.deepcopy(window._data)
    reports = import_demo("")
    assert reports == [] and window._data == before


def test_report_dialog_is_a_scrollable_read_only_text(window, monkeypatch):
    seen = {}

    def fake_exec(dialog):
        view = dialog.findChild(QTextEdit)
        seen.update(text=view.toPlainText(), read_only=view.isReadOnly(), title=dialog.windowTitle())
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec_", fake_exec)
    window._show_conversion_report("Micro-Manager import", "line one\nline two")

    assert seen == {"text": "line one\nline two", "read_only": True, "title": "Micro-Manager import"}


# ---------------------------------------------------------------- label picker
def _form(setup: dict, category: str, name: str):
    """A property editor with ``setup``'s device loaded, as the window does it."""
    panel = editor.PropertyEditor()
    device, _enabled = editor._locate_device(setup, category, name)
    panel.load_device(category, name, device, setup=setup)
    return panel


def _combo_texts(field_widget) -> list[str]:
    combo = field_widget._w
    return [combo.itemText(i) for i in range(combo.count())]


def test_camera_label_offers_cameras_of_the_hub_cfg_not_stages(qapp):
    setup = _demo_template()              # configFile is the relative "MMConfig_demo.cfg"
    panel = _form(setup, "detectors", "Camera")

    label = panel._field_widgets[("props", "label")]
    assert label._w.isEditable()
    assert "Camera" in _combo_texts(label)
    assert "Z" not in _combo_texts(label)
    assert "XY" not in _combo_texts(label)
    assert label.get_value() == "Camera"


def test_stage_label_offers_stages_and_flip_mirror_label_offers_state_devices(qapp):
    setup = _demo_template()
    stage = _form(setup, "positioners", "Z")._field_widgets[("props", "label")]
    assert {"Z", "XY"} <= set(_combo_texts(stage))
    assert "Camera" not in _combo_texts(stage)

    flip = _form(setup, "flipMirrors", "Dichroic")._field_widgets[("props", "label")]
    assert "Dichroic" in _combo_texts(flip)
    assert "Camera" not in _combo_texts(flip) and "Z" not in _combo_texts(flip)


def test_light_label_follows_the_enable_kind(qapp):
    setup = _demo_template()
    light = setup["lasers"]["LED"]
    light["managerProperties"]["enable"] = {"kind": "shutter"}
    shutters = _form(setup, "lasers", "LED")._field_widgets[("props", "label")]
    assert "LED Shutter" in _combo_texts(shutters)
    assert "Dichroic" not in _combo_texts(shutters)

    light["managerProperties"]["enable"] = {"kind": "property", "name": "Intensity",
                                            "on": 1, "off": 0}
    anything = _form(setup, "lasers", "LED")._field_widgets[("props", "label")]
    assert {"Camera", "Z", "Dichroic"} <= set(_combo_texts(anything))


def test_without_adapters_every_label_is_offered(qapp, monkeypatch):
    monkeypatch.setattr(convert, "bare_core", lambda *a, **k: (_ for _ in ()).throw(
        convert.MMAdaptersNotFoundError("none")))
    editor._MM_CFG_CACHE.clear()
    try:
        labels, note = editor._micromanager_label_choices(
            _demo_template(), "MMCore", "micromanager.camera")
    finally:
        editor._MM_CFG_CACHE.clear()

    assert {"Camera", "Z", "XY", "Dichroic"} <= set(labels)
    assert "device types unavailable" in note


def test_rs232device_lists_the_micromanager_hubs_only(qapp):
    setup = _demo_template()
    setup["rs232devices"]["Serial"] = {"managerName": "RS232Manager", "managerProperties": {}}
    setup["rs232devices"]["Second"] = copy.deepcopy(setup["rs232devices"]["MMCore"])

    hub = _form(setup, "detectors", "Camera")._field_widgets[("props", "rs232device")]

    assert _combo_texts(hub) == ["", "MMCore", "Second"]
    assert hub.get_value() == "MMCore"


def test_no_hub_configured_leaves_free_text_and_a_hint(qapp):
    setup = {"detectors": {"Camera": {
        "managerName": "micromanager.camera",
        "managerProperties": {"rs232device": "", "label": "Camera"}}}}
    panel = _form(setup, "detectors", "Camera")

    label = panel._field_widgets[("props", "label")]
    assert _combo_texts(label) == ["", "Camera"]                    # only what is already there
    assert label._w.isEditable()
    assert "hub" in label._w.lineEdit().placeholderText()
    assert label.get_value() == "Camera"


def test_hub_without_config_file_or_with_unreadable_one_says_so(qapp, tmp_path):
    setup = _demo_template()
    setup["rs232devices"]["MMCore"]["managerProperties"]["configFile"] = None
    assert "no configFile" in editor._micromanager_label_choices(
        setup, "MMCore", "micromanager.camera")[1]

    setup["rs232devices"]["MMCore"]["managerProperties"]["configFile"] = str(tmp_path / "gone.cfg")
    labels, note = editor._micromanager_label_choices(setup, "MMCore", "micromanager.camera")
    assert labels == [] and "was not found" in note


def test_picking_another_hub_refills_the_labels(qapp, tmp_path):
    setup = _demo_template()
    other = tmp_path / "other.cfg"
    other.write_text("Device,Cam2,DemoCamera,DCam\nProperty,Core,Camera,Cam2\n"
                     "Property,Core,Initialize,1\n", encoding="utf-8")
    setup["rs232devices"]["Other"] = {
        "managerName": HUB, "managerProperties": {"configFile": str(other)}}
    panel = _form(setup, "detectors", "Camera")
    hub = panel._field_widgets[("props", "rs232device")]
    label = panel._field_widgets[("props", "label")]
    assert "Camera" in _combo_texts(label)

    hub._w.setCurrentText("Other")

    # The other hub's cameras; "Camera" stays only as the value the file has
    # (a dangling value is kept selectable, never silently dropped).
    assert _combo_texts(label) == ["", "Cam2", "Camera"]
    assert label._w.currentText() == "Camera"
    assert label.is_touched() is False


def test_label_values_round_trip_through_apply(qapp):
    setup = _demo_template()
    setup["detectors"]["Camera"]["managerProperties"]["label"] = "Gone"   # not in the cfg
    panel = _form(setup, "detectors", "Camera")
    applied = []
    panel.sig_apply.connect(lambda cat, name, device: applied.append(device))

    panel._do_apply()

    assert applied[0] == setup["detectors"]["Camera"]
    assert "Gone" in _combo_texts(panel._field_widgets[("props", "label")])


def test_picking_a_label_is_an_edit_and_is_written(qapp):
    setup = _demo_template()
    panel = _form(setup, "positioners", "Z")
    label = panel._field_widgets[("props", "label")]
    label._w.setCurrentIndex(label._w.findText("XY"))
    label._w.activated.emit(label._w.currentIndex())
    applied = []
    panel.sig_apply.connect(lambda cat, name, device: applied.append(device))

    panel._do_apply()

    assert applied[0]["managerProperties"]["label"] == "XY"


def test_hub_manager_form_is_unchanged(qapp):
    setup = _demo_template()
    panel = _form(setup, "rs232devices", "MMCore")
    assert ("props", "label") not in panel._field_widgets
    assert panel._field_widgets[("props", "configFile")]._def["type"] != "ref"


# --------------------------------------------------------- consistency checks
def test_label_types_mirror_the_managers_bridge_device_types():
    from imswitch_device_micromanager.camera import MicroManagerCameraManager
    from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager
    from imswitch_device_micromanager.stage import MicroManagerStageManager

    managers = {
        "micromanager.camera": MicroManagerCameraManager,
        "micromanager.stage": MicroManagerStageManager,
        "micromanager.flip-mirror": MicroManagerFlipMirrorManager,
    }
    assert set(managers) == set(editor.MM_LABEL_TYPES)
    for manager_id, cls in managers.items():
        assert tuple(editor.MM_LABEL_TYPES[manager_id]) == tuple(cls.BRIDGE_DEVICE_TYPES)


def test_the_bridge_template_lists_without_errors_in_the_palette(window):
    left = window._left
    micromanager = [t for t in left._plugin_templates if t.plugin_name == "imswitch-device-micromanager"]
    assert {t.category for t in micromanager} == {
        "rs232devices", "detectors", "positioners", "lasers", "flipMirrors"}
    assert [e for e in left._plugin_template_errors
            if e.plugin_name == "imswitch-device-micromanager"] == []

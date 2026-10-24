"""The ``.cfg`` converter against the real ``MMConfig_demo.cfg`` and the real adapter libraries.

``MMConfig_demo.cfg`` ships in the ``mm-test-adapters`` folder. Devices (13): ``DHub`` (Hub),
``Camera``, ``Z`` (Stage), ``XY`` (XYStage), ``White Light Shutter`` and ``LED Shutter`` (Shutters),
``Dichroic`` / ``Emission`` / ``Excitation`` (10-position wheels), ``Objective`` (6), ``Path`` (3),
``LED`` (State, 10 positions through the pre-init property), ``Autofocus`` (AutoFocus). The demo has
no two-position device, so those cases use small ``.cfg`` texts of their own.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from imswitch_device_micromanager import convert
from imswitch_device_micromanager._core import MMAdaptersNotFoundError
from imswitch_device_micromanager.convert import (
    CfgParseError, apply_type_overrides, build_setup, classify, main, parse_cfg, parse_cfg_text,
)
from mm_helpers import DEMO_CONFIG

DEMO_DEVICES = 13


@pytest.fixture(scope="module")
def demo_cfg_path():
    import mm_test_adapters

    return str(Path(mm_test_adapters.device_adapter_path()) / DEMO_CONFIG)


@pytest.fixture(scope="module")
def demo_model(demo_cfg_path):
    return parse_cfg(demo_cfg_path)


@pytest.fixture(scope="module")
def demo_types(demo_model):
    return classify(demo_model)


def type_names(types):
    return {label: (None if t is None else convert.type_name(t)) for label, t in types.items()}


FLIP_CFG = """\
Property,Core,Initialize,0
Device,Hub,DemoCamera,DHub
Device,Flip,DemoCamera,DStateDevice
Device,Mount,DemoCamera,DWheel
Device,Wheel3,DemoCamera,DWheel
Device,Lonely,DemoCamera,DStateDevice
Device,Clash,DemoCamera,DStateDevice
Property,Flip,Number of positions,2
Property,Wheel3,Number of positions,3
Property,Clash,Number of positions,2
Parent,Flip,Hub
Property,Core,Initialize,1
Label,Mount,1,Out
Label,Mount,0,In
Label,Clash,4,Five
"""


# ---------------------------------------------------------------------- parser
def test_the_demo_cfg_parses_into_its_devices_and_parents(demo_model):
    assert len(demo_model.devices) == DEMO_DEVICES
    assert list(demo_model.devices)[:3] == ["DHub", "Camera", "Dichroic"]
    camera = demo_model.devices["Camera"]
    assert (camera.library, camera.name, camera.parent) == ("DemoCamera", "DCam", "DHub")
    assert demo_model.devices["LED Shutter"].parent is None       # the Utilities shutter has no hub
    assert demo_model.devices["LED Shutter"].library == "Utilities"
    assert demo_model.devices["LED Shutter"].name == "State Device Shutter"
    assert demo_model.devices["DHub"].parent is None
    assert sum(1 for d in demo_model.devices.values() if d.parent == "DHub") == 11


def test_properties_before_the_initialize_marker_are_pre_init(demo_model):
    assert demo_model.devices["LED"].pre_init == {"Number of positions": "10"}
    assert demo_model.devices["Camera"].pre_init == {"MaximumExposureMs": "10000.0"}
    assert all(not d.properties for d in demo_model.devices.values())
    assert "Initialize" not in demo_model.core_properties


def test_the_demo_cfg_roles_labels_groups_and_pixel_sizes(demo_model):
    assert demo_model.roles == {"Camera": "Camera", "Shutter": "White Light Shutter", "Focus": "Z"}
    assert demo_model.core_properties["AutoShutter"] == "1"
    assert demo_model.focus_directions == {"Z": "0"}
    assert demo_model.devices["Dichroic"].state_labels[0] == "400DCLP"
    assert len(demo_model.devices["Dichroic"].state_labels) == 10
    assert set(demo_model.config_groups) == {"Camera", "Objective", "Channel", "Channel-Multiband", "System", "LightPath"}
    assert demo_model.config_groups["Objective"]["20X"] == [("Objective", "State", "3")]
    assert {n: p.size_um for n, p in demo_model.pixel_sizes.items()} == {"Res10x": 1.0, "Res20x": 0.5, "Res40x": 0.25}
    assert demo_model.pixel_sizes["Res20x"].settings == [("Objective", "Label", "Nikon 20X Plan Fluor ELWD")]
    assert demo_model.warnings == []


def test_properties_after_the_marker_are_not_pre_init_and_a_reset_starts_over():
    model = parse_cfg_text("Device,A,DemoCamera,DCam\nProperty,A,Early,1\nProperty,Core,Initialize,1\n"
                           "Property,A,Late,2\nProperty,Core,Initialize,0\nProperty,A,Again,3\n")
    assert model.devices["A"].pre_init == {"Early": "1", "Again": "3"}
    assert model.devices["A"].properties == {"Late": "2"}


def test_comments_blank_lines_crlf_and_empty_values_are_read_like_mmcore_reads_them():
    model = parse_cfg_text("# a comment\r\n\r\nDevice,A,Lib,Name\r\nProperty,A,Empty\r\nProperty,A,Full,x y\r\n")
    assert model.devices["A"].pre_init == {"Empty": "", "Full": "x y"}


def test_an_unknown_command_is_kept_as_a_warning_not_an_error():
    model = parse_cfg_text("Device,A,Lib,Name\nBrandNewThing,A,1\n")
    assert model.warnings == ["line 2: unknown command 'BrandNewThing' ignored"]


@pytest.mark.parametrize("text, line, message", [
    ("Device,A,Lib\n", 1, "Device takes 4 comma-separated fields"),
    ("# ok\n\nDevice,A,Lib,Name,Extra\n", 3, "Device takes 4"),
    ("Device,A,Lib,Name\nParent,A\n", 2, "Parent takes 3"),
    ("Device,A,Lib,Name\nProperty,A,P,v,extra\n", 2, "Property takes 3 or 4"),
    ("Device,A,Lib,Name\nDevice,A,Lib,Name\n", 2, "declared twice (first on line 1)"),
    ("Device,,Lib,Name\n", 1, "needs a label"),
    ("Parent,A,Hub\n", 1, "refers to the device 'A', which no Device line before it declares"),
    ("Device,A,Lib,Name\nProperty,B,P,v\n", 2, "refers to the device 'B'"),
    ("Device,A,Lib,Name\nLabel,A,x,Text\n", 2, "Label needs a whole-number state"),
    ("Device,A,Lib,Name\nLabel,A,-1,Text\n", 2, "Label needs a state >= 0"),
    ("PixelSize_um,Res,big\n", 1, "PixelSize_um needs a number"),
    ("Delay,A\n", 1, "Delay takes 3"),
])
def test_a_malformed_line_is_a_parse_error_naming_the_line(text, line, message):
    with pytest.raises(CfgParseError) as excinfo:
        parse_cfg_text(text, "bad.cfg")
    assert excinfo.value.line_number == line and excinfo.value.path == "bad.cfg"
    assert message in str(excinfo.value) and str(excinfo.value).startswith(f"bad.cfg:{line}:")


def test_position_counts_come_from_the_pre_init_property_or_the_label_lines():
    model = parse_cfg_text(FLIP_CFG)
    count, problem = model.position_count("Flip")
    assert (count.count, problem) == (2, None) and "Number of positions" in count.source
    count, _ = model.position_count("Wheel3")
    assert count.count == 3
    count, _ = model.position_count("Mount")
    assert count.count == 2 and "2 Label line(s)" == count.source
    assert model.position_count("Lonely")[0] is None and "neither" in model.position_count("Lonely")[1]
    count, problem = model.position_count("Clash")
    assert count is None and "says 2 but the Label lines go up to state 4" in problem


# ------------------------------------------------------------- classification
def test_the_demo_devices_are_classified_through_the_adapter_libraries(demo_types):
    assert type_names(demo_types) == {
        "DHub": "Hub", "Camera": "Camera", "Dichroic": "State", "Emission": "State", "Excitation": "State",
        "Objective": "State", "Z": "Stage", "Path": "State", "XY": "XYStage",
        "White Light Shutter": "Shutter", "Autofocus": "AutoFocus", "LED": "State", "LED Shutter": "Shutter"}
    assert demo_types.reasons == {} and demo_types.source.startswith("adapters from ")


def test_classification_loads_nothing_into_a_core_handed_in(demo_model, adapter_dir):
    core, paths, _source = convert.bare_core([adapter_dir])
    types = classify(demo_model, core=core)
    assert core.getLoadedDevices() == ("Core",) and types["Camera"] is not None
    assert types.source == "adapters of the core handed in" and paths == [adapter_dir]


def test_an_unknown_library_or_device_name_is_unresolved_with_the_reason():
    model = parse_cfg_text("Device,Ghost,NoSuchLibrary,Thing\nDevice,Odd,DemoCamera,DNotThere\nDevice,Cam,DemoCamera,DCam\n")
    types = classify(model)
    assert types["Cam"] is not None and types["Ghost"] is None and types["Odd"] is None
    assert "adapter library 'NoSuchLibrary'" in types.reasons["Ghost"] and "NoSuchLibrary" in types.reasons["Ghost"]
    assert "has no device 'DNotThere'" in types.reasons["Odd"] and "'DCam'" in types.reasons["Odd"]


def test_no_adapters_at_all_leaves_every_device_unresolved(monkeypatch, demo_model):
    import pymmcore_plus

    monkeypatch.delenv("MICROMANAGER_PATH", raising=False)
    monkeypatch.setattr(pymmcore_plus, "find_micromanager", lambda *a, **k: None)
    types = classify(demo_model)
    assert set(types) == set(demo_model.devices) and all(t is None for t in types.values())
    assert all("no Micro-Manager device adapters found" in r for r in types.reasons.values())
    assert types.source.startswith("classification unavailable")


def test_adapter_path_resolution_is_the_hubs_explicit_then_env_then_search(monkeypatch, adapter_dir, demo_model):
    explicit = classify(demo_model, adapter_paths=[adapter_dir])
    assert explicit.source == f"adapters from adapterPaths: ['{adapter_dir}']"
    monkeypatch.setenv("MICROMANAGER_PATH", adapter_dir)
    assert classify(demo_model).source == f"adapters from MICROMANAGER_PATH: ['{adapter_dir}']"
    with pytest.raises(MMAdaptersNotFoundError, match="do not exist"):
        convert.bare_core(["/nonexistent/adapters"])
    gone = classify(demo_model, adapter_paths=["/nonexistent/adapters"])       # reported, not raised
    assert all(t is None for t in gone.values()) and "do not exist" in gone.reasons["Camera"]


# ------------------------------------------------------------------ build_setup
def test_build_setup_on_the_demo_maps_camera_stages_and_shutters(demo_model, demo_types, demo_cfg_path):
    setup, report = build_setup(demo_model, demo_types, config_file=demo_cfg_path)
    assert list(setup) == ["rs232devices", "detectors", "positioners", "lasers", "availableWidgets"]
    assert setup["rs232devices"] == {"MMCore": {"managerName": "micromanager.core", "managerProperties": {
        "configFile": demo_cfg_path, "adapterPaths": []}}}
    assert setup["detectors"] == {"Camera": {
        "managerName": "micromanager.camera",
        "managerProperties": {"rs232device": "MMCore", "label": "Camera"}, "forAcquisition": True}}
    assert setup["positioners"]["Z"]["axes"] == ["Z"] and setup["positioners"]["XY"]["axes"] == ["X", "Y"]
    for name in ("Z", "XY"):
        entry = setup["positioners"][name]
        assert entry["managerName"] == "micromanager.stage" and entry["forPositioning"] is True
        assert entry["managerProperties"] == {"rs232device": "MMCore", "label": name}      # homeable stays unset
    assert set(setup["lasers"]) == {"White Light Shutter", "LED Shutter"}
    for label, entry in setup["lasers"].items():
        assert entry == {"managerName": "micromanager.light",
                         "managerProperties": {"rs232device": "MMCore", "label": label, "enable": {"kind": "shutter"}},
                         "valueRangeMin": None, "valueRangeMax": None}
        assert "wavelength" not in entry
    assert "flipMirrors" not in setup                           # the demo has no two-position device
    assert setup["availableWidgets"] == ["Settings", "View", "Recording", "Image", "Positioner", "Laser"]
    json.dumps(setup)                                           # plain data, no comments, no sentinels


def test_the_report_lists_what_was_not_written_and_why(demo_model, demo_types):
    _setup, report = build_setup(demo_model, demo_types)
    text = "\n".join(report)
    for label in ("Dichroic", "Emission", "Excitation", "Objective", "Path", "LED"):
        line = next(l for l in report if l.strip().startswith(f"'{label}'"))
        assert "State" in line and "multi-position State device" in line and "selector device kind" in line
    assert "'Dichroic'  DemoCamera/DWheel  State, 10 positions" in text
    assert "'Objective'  DemoCamera/DObjective  State, 6 positions" in text
    assert "'Path'  DemoCamera/DLightPath  State, 3 positions" in text
    assert "'LED'  DemoCamera/DStateDevice  State, 10 positions (property 'Number of positions')" in text
    assert "'Autofocus'  DemoCamera/DAutoFocus  AutoFocus  not written: no ImSwitch2 contract" in text
    assert "'DHub'  DemoCamera/DHub  Hub  not written" in text
    assert "Not written (8)" in text and f"{DEMO_DEVICES} device(s)" in text
    # nothing written is also listed as skipped, and nothing skipped was written
    setup, _ = build_setup(demo_model, demo_types)
    written_labels = {l for s in ("detectors", "positioners", "lasers") for l in setup[s]}
    assert not any(f"'{label}'  " in text.split("Not written")[1] for label in written_labels)


def test_the_report_says_where_the_pixel_size_comes_from(demo_model, demo_types):
    setup, report = build_setup(demo_model, demo_types)
    text = "\n".join(report)
    assert "cameraPixelSizeUm" not in json.dumps(setup)
    assert ("detectors['Camera']: cameraPixelSizeUm is not written; Micro-Manager's pixel-size config supplies it "
            "(Res20x, Res40x, Res10x)") in text
    assert "6 config group(s), 3 pixel-size config(s)" in text


def test_a_cfg_without_pixel_size_configs_makes_the_pixel_size_a_todo():
    model = parse_cfg_text("Device,Cam,DemoCamera,DCam\n")
    setup, report = build_setup(model, classify(model))
    assert "cameraPixelSizeUm" not in json.dumps(setup)
    todo = [l for l in report if l.strip().startswith("TODO")]
    assert len(todo) == 1 and "detectors['Cam']" in todo[0] and "no pixel-size config" in todo[0]
    assert "set managerProperties.cameraPixelSizeUm" in todo[0]


def test_every_value_the_converter_could_not_derive_is_a_todo_line(demo_model, demo_types):
    _setup, report = build_setup(demo_model, demo_types)
    todos = [l for l in report if l.strip().startswith("TODO")]
    assert len(todos) == 2 and all("wavelength is not written" in t for t in todos)
    assert {t.split("'")[1] for t in todos} == {"White Light Shutter", "LED Shutter"}


def test_a_stage_that_is_not_the_focus_device_gets_a_todo_axis_name():
    model = parse_cfg_text("Device,Focus,DemoCamera,DStage\nDevice,Piezo,DemoCamera,DStage\n"
                           "Device,Table,DemoCamera,DXYStage\nProperty,Core,Focus,Focus\n")
    setup, report = build_setup(model, classify(model))
    assert setup["positioners"]["Focus"]["axes"] == ["Z"]
    assert setup["positioners"]["Piezo"]["axes"] == ["TODO"]
    assert setup["positioners"]["Table"]["axes"] == ["X", "Y"]
    todos = [l for l in report if l.strip().startswith("TODO") and "Piezo" in l]
    assert len(todos) == 1 and 'axes is ["TODO"]' in todos[0] and "Property,Core,Focus" in todos[0]
    assert not any("positioners['Focus']" in l and "TODO" in l for l in report)


def test_two_position_state_devices_become_flip_mirrors_and_others_are_reported():
    model = parse_cfg_text(FLIP_CFG)
    setup, report = build_setup(model, classify(model))
    assert set(setup["flipMirrors"]) == {"Flip", "Mount"}
    assert setup["flipMirrors"]["Flip"] == {
        "managerName": "micromanager.flip-mirror", "managerProperties": {"rs232device": "MMCore", "label": "Flip"}}
    assert setup["availableWidgets"][-1] == "FlipMirror"
    text = "\n".join(report)
    assert "flipMirrors['Flip']  DemoCamera/DStateDevice  State, 2 positions (property 'Number of positions')" in text
    assert "flipMirrors['Mount']  DemoCamera/DWheel  State, 2 positions (2 Label line(s))" in text
    assert "'Wheel3'  DemoCamera/DWheel  State, 3 positions" in text
    assert "'Lonely'  DemoCamera/DStateDevice  State  position count unknown" in text
    assert "add a flipMirrors entry by hand" in text
    assert "'Clash'" in text and "says 2 but the Label lines go up to state 4" in text


def test_several_cameras_and_a_relative_cfg_path_are_noted():
    model = parse_cfg_text("Device,A,DemoCamera,DCam\nDevice,B,DemoCamera,DCam\n")
    setup, report = build_setup(model, classify(model), config_file="rig/MMConfig.cfg", adapter_paths=["/mm"])
    text = "\n".join(report)
    assert "MMCore has one frame buffer" in text and "relative path 'rig/MMConfig.cfg'" in text
    assert setup["rs232devices"]["MMCore"]["managerProperties"] == {"configFile": "rig/MMConfig.cfg", "adapterPaths": ["/mm"]}


def test_a_device_missing_from_the_types_counts_as_unresolved(demo_model):
    setup, report = build_setup(demo_model, {})
    assert list(setup) == ["rs232devices", "availableWidgets"]
    assert sum("type unresolved" in l for l in report) == DEMO_DEVICES


def test_the_hub_name_is_used_everywhere():
    model = parse_cfg_text("Device,Cam,DemoCamera,DCam\n")
    setup, _ = build_setup(model, classify(model), hub_name="Scope")
    assert list(setup["rs232devices"]) == ["Scope"]
    assert setup["detectors"]["Cam"]["managerProperties"]["rs232device"] == "Scope"


# ------------------------------------------------------- overrides / no-classify
def test_type_overrides_supply_and_replace_types(demo_model, demo_types):
    types = apply_type_overrides(demo_model, demo_types, {"Autofocus": "stage", "Path": "SHUTTER"})
    assert convert.type_name(types["Autofocus"]) == "Stage" and convert.type_name(types["Path"]) == "Shutter"
    setup, report = build_setup(demo_model, types)
    assert setup["positioners"]["Autofocus"]["axes"] == ["TODO"] and "Path" in setup["lasers"]
    assert any(l.startswith("Types given with --type: Autofocus=stage, Path=shutter") for l in report)
    assert demo_types["Autofocus"] is not types["Autofocus"]        # the input classification is untouched


@pytest.mark.parametrize("overrides, message", [
    ({"Nope": "camera"}, "declares no device 'Nope'"),
    ({"Camera": "wheel"}, r"must be one of \['camera', 'shutter', 'stage', 'state', 'xystage'\]"),
])
def test_a_bad_type_override_is_refused(demo_model, demo_types, overrides, message):
    with pytest.raises(ValueError, match=message):
        apply_type_overrides(demo_model, demo_types, overrides)


# --------------------------------------------------- the output is a valid setup
def _convert_to(tmp_path, cfg, *extra):
    out = tmp_path / "setup.json"
    code = main(["convert", str(cfg), "-o", str(out), *extra])
    return code, out


@pytest.mark.filterwarnings("ignore:Missing value of non-optional type wavelength")
def test_the_converted_demo_passes_validate_setup_and_loads_as_setup_info(tmp_path, demo_cfg_path, adapter_dir, capsys):
    from imswitch.imcontrol.model.plugins.registry import get_default_registry
    from imswitch.imcontrol.model.plugins.validation import validate_setup_file
    from imswitch.imcontrol.view import ViewSetupInfo

    code, out = _convert_to(tmp_path, demo_cfg_path, "--adapter-path", adapter_dir)
    assert code == 0
    report = validate_setup_file(out, get_default_registry())
    assert not report.has_errors
    # the only findings are the lights' missing wavelengths: the converter's own TODOs
    assert sorted((d.code, d.path[1], d.message) for d in report.diagnostics) == [
        ("kind.schema", "LED Shutter", "(root): 'wavelength' is a required property"),
        ("kind.schema", "White Light Shutter", "(root): 'wavelength' is a required property")]
    info = ViewSetupInfo.from_json(out.read_text(encoding="utf-8"), infer_missing=True)
    assert sorted(info.detectors) == ["Camera"] and sorted(info.positioners) == ["XY", "Z"]
    assert sorted(info.lasers) == ["LED Shutter", "White Light Shutter"]
    assert info.rs232devices["MMCore"].managerProperties["adapterPaths"] == [adapter_dir]
    assert info.lasers["LED Shutter"].wavelength is None and info.lasers["LED Shutter"].valueRangeMin is None


def test_the_converted_flip_mirror_setup_validates_cleanly(tmp_path):
    from imswitch.imcontrol.model.plugins.registry import get_default_registry
    from imswitch.imcontrol.model.plugins.validation import validate_setup_file

    cfg = tmp_path / "flip.cfg"
    cfg.write_text(FLIP_CFG)
    code, out = _convert_to(tmp_path, cfg)
    assert code == 0
    report = validate_setup_file(out, get_default_registry())
    assert not report.has_errors and not report.diagnostics, report.format()


@pytest.mark.filterwarnings("ignore:Missing value of non-optional type wavelength")
def test_the_converted_demo_boots_through_the_managers(tmp_path, demo_cfg_path, adapter_dir):
    """The generated file is a working setup: hub, camera, both stages and both lights come up on the real MMCore."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
    from imswitch.imcontrol.model.managers.LasersManager import LasersManager
    from imswitch.imcontrol.model.managers.PositionersManager import PositionersManager
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager
    from imswitch_device_micromanager.camera import MicroManagerCameraManager
    from imswitch_device_micromanager.light import MicroManagerLightManager
    from imswitch_device_micromanager.stage import MicroManagerStageManager

    code, out = _convert_to(tmp_path, demo_cfg_path, "--adapter-path", adapter_dir)
    assert code == 0
    info = SetupInfo.from_json(out.read_text(encoding="utf-8"), infer_missing=True)
    rs232 = RS232sManager(info.rs232devices)
    detectors = DetectorsManager(info.detectors, updatePeriod=20, rs232sManager=rs232)
    positioners = PositionersManager(info.positioners, rs232sManager=rs232)
    lasers = LasersManager(info.lasers, rs232sManager=rs232)
    try:
        hub = rs232["MMCore"]
        assert hub.isConnected
        assert type(detectors["Camera"]) is MicroManagerCameraManager
        assert detectors["Camera"].pixelSizeUm[1] == 1.0            # Micro-Manager's Res10x: not in the file
        assert type(positioners["Z"]) is MicroManagerStageManager and positioners["XY"].axes == ["X", "Y"]
        positioners["Z"].setPosition(12.5, "Z")
        assert hub.host.core.getPosition("Z") == 12.5
        for name in ("White Light Shutter", "LED Shutter"):
            assert type(lasers[name]) is MicroManagerLightManager and lasers[name].isBinary
        assert lasers["LED Shutter"].setEnabled(True) and hub.host.core.getShutterOpen("LED Shutter")
        lasers["LED Shutter"].setEnabled(False)
    finally:
        for manager in (lasers, positioners, detectors):
            for name in manager.getAllDeviceNames():
                manager[name].finalize()
        rs232.finalize()


def test_the_converted_two_position_device_boots_as_a_flip_mirror(tmp_path, adapter_dir):
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.FlipMirrorsManager import FlipMirrorsManager
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager
    from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager

    cfg = tmp_path / "flip.cfg"
    cfg.write_text("Property,Core,Initialize,0\nDevice,Hub,DemoCamera,DHub\nDevice,Flip,DemoCamera,DStateDevice\n"
                   "Property,Flip,Number of positions,2\nParent,Flip,Hub\nProperty,Core,Initialize,1\n"
                   "Label,Flip,1,Camera\nLabel,Flip,0,Eyepiece\n")
    code, out = _convert_to(tmp_path, cfg, "--adapter-path", adapter_dir)
    assert code == 0
    info = SetupInfo.from_json(out.read_text(encoding="utf-8"), infer_missing=True)
    rs232 = RS232sManager(info.rs232devices)
    mirrors = FlipMirrorsManager(info.flipMirrors, rs232sManager=rs232)
    try:
        mirror = mirrors["Flip"]
        assert type(mirror) is MicroManagerFlipMirrorManager and mirror.is_connected()
        assert mirror.get_state_names() == {0: "Eyepiece", 1: "Camera"}
        mirror.move_to(1)
        assert rs232["MMCore"].host.core.getStateLabel("Flip") == "Camera"
    finally:
        mirrors["Flip"].finalize()
        rs232.finalize()


# ------------------------------------------------------------------------- CLI
def test_the_cli_writes_the_setup_and_the_sidecar_report_and_prints_the_report(tmp_path, demo_cfg_path, capsys):
    code, out = _convert_to(tmp_path, demo_cfg_path)
    assert code == 0
    report_file = tmp_path / "setup.conversion-report.txt"
    assert out.is_file() and report_file.is_file()
    printed = capsys.readouterr().out
    assert report_file.read_text(encoding="utf-8").strip() in printed
    assert "Not written (8)" in printed and f"Wrote {out} and {report_file}" in printed
    assert json.loads(out.read_text(encoding="utf-8"))["rs232devices"]["MMCore"]["managerProperties"]["configFile"] == demo_cfg_path


def test_the_cli_writes_the_cfg_path_as_given(tmp_path, demo_cfg_path, monkeypatch, capsys):
    monkeypatch.chdir(Path(demo_cfg_path).parent)
    code, out = _convert_to(tmp_path, DEMO_CONFIG)
    assert code == 0
    assert json.loads(out.read_text())["rs232devices"]["MMCore"]["managerProperties"]["configFile"] == DEMO_CONFIG
    assert "relative path 'MMConfig_demo.cfg'" in capsys.readouterr().out


def test_no_classify_leaves_every_device_unresolved_and_the_json_has_only_the_hub(tmp_path, demo_cfg_path, capsys):
    code, out = _convert_to(tmp_path, demo_cfg_path, "--no-classify")
    assert code == 0
    setup = json.loads(out.read_text(encoding="utf-8"))
    assert list(setup) == ["rs232devices", "availableWidgets"]
    printed = capsys.readouterr().out
    assert f"Not written ({DEMO_DEVICES})" in printed and printed.count("type unresolved: type unknown (--no-classify)") == DEMO_DEVICES
    assert "classification skipped (--no-classify)" in printed


def test_no_classify_with_type_overrides_writes_just_those_devices(tmp_path, demo_cfg_path, capsys):
    code, out = _convert_to(tmp_path, demo_cfg_path, "--no-classify", "--type", "Camera=camera", "--type", "Z=stage")
    setup = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and list(setup["detectors"]) == ["Camera"] and setup["positioners"]["Z"]["axes"] == ["Z"]
    assert "lasers" not in setup


def test_the_cli_type_override_changes_the_written_setup(tmp_path, demo_cfg_path):
    code, out = _convert_to(tmp_path, demo_cfg_path, "--type", "Autofocus=stage")
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["positioners"]["Autofocus"]["axes"] == ["TODO"]


def test_exit_code_is_zero_even_with_todos(tmp_path, demo_cfg_path):
    code, out = _convert_to(tmp_path, demo_cfg_path)
    assert code == 0 and "TODO" in (tmp_path / "setup.conversion-report.txt").read_text(encoding="utf-8")


def test_a_malformed_cfg_line_is_exit_2_naming_the_line_and_writes_nothing(tmp_path, capsys):
    cfg = tmp_path / "bad.cfg"
    cfg.write_text("# header\nDevice,A,DemoCamera,DCam\nDevice,B,DemoCamera\n")
    code, out = _convert_to(tmp_path, cfg)
    assert code == 2 and not out.exists() and not (tmp_path / "setup.conversion-report.txt").exists()
    err = capsys.readouterr().err
    assert f"{cfg}:3:" in err and "Device takes 4 comma-separated fields" in err


def test_a_missing_cfg_or_a_bad_type_or_an_unwritable_output_is_exit_2(tmp_path, demo_cfg_path, capsys):
    assert _convert_to(tmp_path, tmp_path / "missing.cfg")[0] == 2
    assert "error:" in capsys.readouterr().err
    assert _convert_to(tmp_path, demo_cfg_path, "--type", "Nope=camera")[0] == 2
    assert "declares no device 'Nope'" in capsys.readouterr().err
    assert _convert_to(tmp_path, demo_cfg_path, "--type", "Camera=wheel")[0] == 2
    assert _convert_to(tmp_path, demo_cfg_path, "--type", "Camera")[0] == 2
    assert "must look like LABEL=TYPE" in capsys.readouterr().err
    out = tmp_path / "no" / "such" / "dir" / "setup.json"
    assert main(["convert", demo_cfg_path, "-o", str(out)]) == 2
    assert "cannot write the output" in capsys.readouterr().err


def test_python_dash_m_converts_without_importing_imswitch(tmp_path, demo_cfg_path):
    """The command line tool is plain Python: no imswitch import (which would sync ~/ImSwitchConfig), no Qt."""
    out = tmp_path / "setup.json"
    src = Path(convert.__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=os.pathsep.join([str(src), os.environ.get("PYTHONPATH", "")]))
    probe = ("import sys, runpy; sys.argv = ['imswitch_device_micromanager', 'convert', %r, '-o', %r];"
             "\ntry:\n runpy.run_module('imswitch_device_micromanager', run_name='__main__')\n"
             "except SystemExit as e:\n print('EXIT', e.code)\n"
             "print('IMPORTED', [m for m in ('imswitch', 'PyQt5', 'qtpy', 'napari') if m in sys.modules])"
             % (demo_cfg_path, str(out)))
    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "EXIT 0" in result.stdout and "IMPORTED []" in result.stdout, result.stdout + result.stderr
    assert out.is_file() and (tmp_path / "setup.conversion-report.txt").is_file()

"""Phase 6: the extraction tool writes a plugin's schemas into the plugin's own package.

``--package <name>`` locates the installed package without importing it,
reads the managers its ``imswitch.json`` declares, extracts their
``managerProperties`` contracts from the package's own source, and writes
``schemas/managers/<id>.json`` (plus fixtures and an index) beside them --
where ``manager_properties_schema`` can point, so ImSwitch validates and
edits against it through the ordinary contribution path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_TOOL = _REPO / "tools" / "extract_manager_schemas.py"

MANIFEST = {
    "name": "imswitch-vendor-plugin",
    "display_name": "Vendor",
    "schema_version": "0.1",
    "imswitch_min_version": "0.1",
    "license": "GPL-3.0-or-later",
    "contributions": {"device_managers": [
        {"id": "vendor.cam", "kind": "detector", "display_name": "Vendor camera",
         "python_name": "vendor_plugin.detectors:VendorCamManager",
         "manager_name_aliases": ["VendorCamManager"],
         "manager_properties_schema": "schemas/managers/vendor.cam.json"},
        {"id": "vendor.stage", "kind": "positioner", "display_name": "Vendor stage",
         "python_name": "vendor_plugin.stages:VendorStageManager"},
        {"id": "vendor.ghost", "kind": "laser", "display_name": "Not in the tree",
         "python_name": "vendor_plugin.lasers:GhostManager"},
    ]},
}

DETECTORS = '''
    from pathlib import Path

    class VendorCamManager:
        def __init__(self, detectorInfo, name, **lowLevelManagers):
            props = detectorInfo.managerProperties
            self.serial = props["serial"]
            self.exposure = float(props.get("exposureMs", 10.0))
            self.calib = Path(props.get("calibFile", "calib.json"))
            try:
                self.gain = props["gain"]
            except KeyError:
                self.gain = 1
'''
STAGES = '''
    class VendorStageManager:
        def __init__(self, positionerInfo, name, **lowLevelManagers):
            self.port = positionerInfo.managerProperties.get("port", "COM1")
'''


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "vendor_plugin"
    root.mkdir()
    (root / "__init__.py").write_text("raise RuntimeError('the tool must not import the plugin')\n", encoding="utf-8")
    (root / "imswitch.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    (root / "detectors.py").write_text(textwrap.dedent(DETECTORS), encoding="utf-8")
    (root / "stages.py").write_text(textwrap.dedent(STAGES), encoding="utf-8")
    return root


def _run(*args, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(cwd) + os.pathsep + str(_REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run([sys.executable, str(_TOOL), *args], capture_output=True, text=True, cwd=str(_REPO), env=env)


def test_write_then_check_into_the_plugin_package(package):
    result = _run("--package", "vendor_plugin", "--write", cwd=package.parent)
    assert result.returncode == 0, result.stderr
    assert "no class found in the package (no schema)" not in result.stdout or "vendor.ghost" in result.stdout
    schemas = package / "schemas"
    cam = json.loads((schemas / "managers" / "vendor.cam.json").read_text(encoding="utf-8"))
    assert cam["title"] == "vendor.cam managerProperties"
    assert cam["required"] == ["serial"]
    assert cam["properties"]["calibFile"]["type"] == "string", "Path() proves a string"
    assert cam["properties"]["gain"]["x-imswitch-source"] == ["code:optional(try/except KeyError)"]
    assert cam["properties"]["exposureMs"]["x-imswitch-kind"] == "number"
    assert cam["x-imswitch-classes"] == ["VendorCamManager"]
    stage = json.loads((schemas / "managers" / "vendor.stage.json").read_text(encoding="utf-8"))
    assert list(stage["properties"]) == ["port"]
    assert not (schemas / "managers" / "vendor.ghost.json").exists()
    index = json.loads((schemas / "index.json").read_text(encoding="utf-8"))
    assert index["unresolved"] == ["vendor.ghost"]
    assert index["managers"]["vendor.cam"]["category"] == "detectors"
    assert "kinds" not in index, "kind schemas belong to the core tree"
    fixture = json.loads((schemas / "fixtures" / "vendor.cam.json").read_text(encoding="utf-8"))
    assert fixture["category"] == "detectors" and fixture["device"]["managerName"] == "vendor.cam"

    check = _run("--package", "vendor_plugin", "--check", cwd=package.parent)
    assert check.returncode == 0, check.stdout + check.stderr
    (package / "detectors.py").write_text(textwrap.dedent(DETECTORS).replace('"gain"', '"gainDb"'), encoding="utf-8")
    check = _run("--package", "vendor_plugin", "--check", cwd=package.parent)
    assert check.returncode == 1 and "changed: managers/vendor.cam.json" in check.stdout


def test_the_written_schema_is_what_the_contribution_path_resolves(package):
    assert _run("--package", "vendor_plugin", "--write", cwd=package.parent).returncode == 0
    from imswitch.imcontrol.model.plugins.manifest import parse_manifest
    from imswitch.imcontrol.model.plugins.validation import schema_for

    # The tool never imported the package (its __init__ raises); ImSwitch at
    # runtime does, through importlib.resources -- so make it importable now.
    (package / "__init__.py").write_text("", encoding="utf-8")
    sys.path.insert(0, str(package.parent))
    try:
        [cam] = [c for c in parse_manifest(MANIFEST, plugin_name="imswitch-vendor-plugin", plugin_version=None,
                                           source_package="vendor_plugin") if c.id == "vendor.cam"]
        schema = schema_for("detector", "vendor.cam", cam)
        assert schema is not None and schema["required"] == ["serial"]
    finally:
        sys.path.remove(str(package.parent))
        sys.modules.pop("vendor_plugin", None)


def test_a_package_override_is_merged_and_never_overwritten(package):
    overrides = package / "schemas" / "overrides"
    overrides.mkdir(parents=True)
    (overrides / "vendor.cam.json").write_text('{"properties": {"exposureMs": {"type": "number", "minimum": 0}}}', encoding="utf-8")
    assert _run("--package", "vendor_plugin", "--write", cwd=package.parent).returncode == 0
    cam = json.loads((package / "schemas" / "managers" / "vendor.cam.json").read_text(encoding="utf-8"))
    assert cam["properties"]["exposureMs"]["minimum"] == 0 and "override" in cam["properties"]["exposureMs"]["x-imswitch-source"]
    assert (overrides / "vendor.cam.json").read_text(encoding="utf-8").startswith('{"properties"')


def test_report_mode_lists_the_plugins_managers(package):
    result = _run("--package", "vendor_plugin", "--report", "--properties", cwd=package.parent)
    assert result.returncode == 0, result.stderr
    assert "vendor.cam" in result.stdout and "serial" in result.stdout
    assert "vendor.ghost" in result.stdout


def test_a_missing_manifest_or_package_is_an_error(package):
    (package / "imswitch.json").unlink()
    result = _run("--package", "vendor_plugin", "--write", cwd=package.parent)
    assert result.returncode != 0 and "imswitch.json" in result.stderr
    result = _run("--package", "no_such_package", "--write", cwd=package.parent)
    assert result.returncode != 0 and "not installed" in result.stderr


# ── review of PR #35: identities, and parents that must not run ───────────
SAME_NAME = '''
    class CameraManager:
        def __init__(self, detectorInfo, name, **lowLevelManagers):
            self.serial = detectorInfo.managerProperties["serial_{mod}"]
'''


def _manifest(name, contributions):
    return {"name": name, "display_name": name, "schema_version": "0.1", "imswitch_min_version": "0.1",
            "license": "GPL-3.0-or-later", "contributions": {"device_managers": contributions}}


def test_two_classes_of_one_name_get_their_own_schemas_and_check_tells_them_apart(tmp_path):
    root = tmp_path / "twin_plugin"
    root.mkdir()
    (root / "__init__.py").write_text("raise RuntimeError('the tool must not import the plugin')\n", encoding="utf-8")
    for mod in ("a", "b"):
        (root / f"{mod}.py").write_text(textwrap.dedent(SAME_NAME.format(mod=mod)), encoding="utf-8")
    (root / "imswitch.json").write_text(json.dumps(_manifest("twin", [
        {"id": f"twin.{mod}", "kind": "detector", "display_name": mod,
         "python_name": f"twin_plugin.{mod}:CameraManager"} for mod in ("a", "b")
    ])), encoding="utf-8")
    assert _run("--package", "twin_plugin", "--write", cwd=tmp_path).returncode == 0
    managers = root / "schemas" / "managers"
    schema_a = json.loads((managers / "twin.a.json").read_text(encoding="utf-8"))
    schema_b = json.loads((managers / "twin.b.json").read_text(encoding="utf-8"))
    assert schema_a["required"] == ["serial_a"] and schema_b["required"] == ["serial_b"]
    assert _run("--package", "twin_plugin", "--check", cwd=tmp_path).returncode == 0
    # What the old resolution wrote for b: a's contract. --check must object.
    (managers / "twin.b.json").write_text((managers / "twin.a.json").read_text(encoding="utf-8")
                                          .replace("twin.a managerProperties", "twin.b managerProperties"),
                                          encoding="utf-8")
    check = _run("--package", "twin_plugin", "--check", cwd=tmp_path)
    assert check.returncode == 1 and "changed: managers/twin.b.json" in check.stdout


def test_a_dotted_package_is_found_without_running_any_init(tmp_path):
    """``find_spec("hardware_vendor.plugin")`` executes ``hardware_vendor/__init__.py``;
    a hardware vendor's may import its SDK. Neither ``__init__`` may run."""
    parent = tmp_path / "hardware_vendor"
    plugin = parent / "plugin"
    plugin.mkdir(parents=True)
    (parent / "__init__.py").write_text("raise RuntimeError('parent __init__ executed')\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("raise RuntimeError('plugin __init__ executed')\n", encoding="utf-8")
    (plugin / "cams.py").write_text(textwrap.dedent(SAME_NAME.format(mod="hw")), encoding="utf-8")
    (plugin / "imswitch.json").write_text(json.dumps(_manifest("hw", [
        {"id": "hw.cam", "kind": "detector", "display_name": "cam",
         "python_name": "hardware_vendor.plugin.cams:CameraManager"},
    ])), encoding="utf-8")
    for mode in ("--report", "--write", "--check"):
        result = _run("--package", "hardware_vendor.plugin", mode, cwd=tmp_path)
        assert result.returncode == 0, (mode, result.stderr)
        assert "__init__ executed" not in result.stderr
    schema = json.loads((plugin / "schemas" / "managers" / "hw.cam.json").read_text(encoding="utf-8"))
    assert schema["required"] == ["serial_hw"]
    missing = _run("--package", "hardware_vendor.nothing", "--report", cwd=tmp_path)
    assert missing.returncode != 0 and "not installed" in missing.stderr
    assert "__init__ executed" not in missing.stderr


def test_the_package_locator_never_imports(tmp_path, monkeypatch):
    """In-process too: the spec is found and nothing lands in sys.modules."""
    import importlib.util as ilu

    # The real package first: loading the tool into a process that has not
    # imported it would install the tool's light stand-in for later tests.
    import imswitch.imcontrol.model  # noqa: F401
    spec = ilu.spec_from_file_location("extract_manager_schemas_under_test", _TOOL)
    tool = ilu.module_from_spec(spec)
    spec.loader.exec_module(tool)
    parent = tmp_path / "noisy_vendor"
    (parent / "plugin").mkdir(parents=True)
    (parent / "__init__.py").write_text("raise RuntimeError('parent __init__ executed')\n", encoding="utf-8")
    (parent / "plugin" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = tool.find_package_spec("noisy_vendor.plugin")
    assert found is not None and Path(next(iter(found.submodule_search_locations))) == parent / "plugin"
    assert "noisy_vendor" not in sys.modules and "noisy_vendor.plugin" not in sys.modules
    assert tool.find_package_spec("noisy_vendor.missing") is None
    assert tool.find_package_spec("not a package") is None

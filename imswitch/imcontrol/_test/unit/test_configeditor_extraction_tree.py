"""Extraction over the real manager tree: the Phase 0 exit criterion.

The rule tests pin what the extractor may conclude from a snippet. These pin
what it concludes from ImSwitch itself -- the numbers the plan quotes, the
managers it names, and a checked-in snapshot that fails the moment a manager
reads a new key without the schemas being regenerated (Phase 1 turns that
into the drift guard; here it already tells you *which* manager moved).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import extraction as ex
from imswitch.imcontrol.model.configeditor.catalog import build_catalog
from imswitch.imcontrol.model.plugins.registry import build_default_registry

_REPO = Path(__file__).resolve().parents[4]
MANAGERS_ROOT = _REPO / "imswitch" / "imcontrol" / "model" / "managers"
SETUPS_DIR = _REPO / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
DOCS_DIR = _REPO / "docs" / "devices"
SNAPSHOT = Path(__file__).with_name("configeditor_extraction_snapshot.json")
TOOL = _REPO / "tools" / "extract_manager_schemas.py"


def core_catalog():
    """The core catalog: built-in registry, discovery off, explicit root.

    What the tool uses, so the figures do not depend on plugins installed on
    the machine running the tests.
    """
    registry = build_default_registry(discover=False)
    python_names = {c.id: c.python_name for c in registry.list_contributions()}
    catalog = build_catalog(registry=registry, managers_root=MANAGERS_ROOT)
    return {info.manager_name: python_names.get(info.manager_name) for info in catalog.managers()}


@pytest.fixture(scope="module")
def extractions():
    catalog = core_catalog()
    return ex.extract_managers(
        sorted(catalog), managers_root=MANAGERS_ROOT, setups_dir=SETUPS_DIR, docs_dir=DOCS_DIR,
        class_names={name: python_name and python_name.rsplit(":", 1)[1]
                     for name, python_name in catalog.items()},
    )


@pytest.fixture(scope="module")
def report(extractions):
    return ex.coverage_report(extractions, docs=ex.docs_cards(DOCS_DIR))


# ── the numbers the plan quotes ───────────────────────────────────────────
def test_the_coverage_the_plan_is_built_on(report):
    # 65 until the four camera managers whose drivers were never in the tree (Basler, ESP32Cam, GXPIPY, JetsonCam) were removed by the magic-number audit.
    assert report.managers == 61, "60 plus RS232Manager, which the legacy scan no longer skips"
    # 58 after the first review: helper call sites (LaserManager.getProperty,
    # ThorlabsMFF._read_info), module and method functions handed the dict
    # (DetectorManager.configuredCameraPixelSize), and Info parameters of
    # methods other than __init__ are all followed now.
    # ... plus RS232Manager itself, once the catalog stopped skipping it.
    # Four fewer since the removed camera managers (see above): 59 -> 55.
    assert report.reads_any == 55
    assert report.with_keys == 55
    # Nine of the spellings are APD/PMT snake_case aliases of camelCase
    # properties and fold into one property each.
    # 218 before the merge with the acquisition-layout branch: the removed
    # camera managers took 12 keys, the PMT's aiVoltageMin/aiVoltageMax and
    # the Thorlabs camera's frameBufferDepth added 3. 210: AAAOTF's
    # useMockOnFailure.
    assert report.keys == 210
    assert report.alias_spellings == 9
    # 70 before the removed camera managers took their 8 required keys.
    assert report.required == 62, "60 under the guard-aware rule, plus RS232Manager's port and recv_termination"
    assert report.optional == 148
    assert report.refs == 14
    assert report.none_default_only == 28  # 31 with the removed camera managers
    # 117/118 until Phase 5: PiezoconceptZManager2's card is read as its own
    # (range_um belongs to it), and the docs drift test made every card list
    # every property its manager reads -- 17 rows added, all agreeing.
    # 135 before the removed camera managers' cards went with them (129), plus
    # the PMT's aiVoltageMax, which shared a row with aiVoltageMin and so was
    # never counted as documented. 131: AAAOTF's useMockOnFailure.
    assert (report.docs_agree, report.docs_documented) == (131, 131)


def test_kinds_come_from_code_then_examples_then_docs(report):
    # 92: the PMT's aiVoltageMin/aiVoltageMax are read with a numeric default.
    # 93: AAAOTF's useMockOnFailure is read with a bool default.
    assert report.typed_by_code == 93
    assert report.typed_with_examples > report.typed_by_code
    assert report.typed_with_docs > report.typed_with_examples
    assert report.typed_with_docs <= report.keys


def test_no_selectable_manager_reads_another_devices_properties(report):
    """The strict receiver rule discards only TriggerScopeManager, which is not selectable."""
    assert report.discarded == []


def test_the_tree_has_no_uncertain_requiredness_today(report):
    """Not a rule -- a fact about the tree, so a future one is noticed."""
    assert report.uncertain_keys == []


def test_the_only_unresolved_reads_are_dynamic_sub_keys(report):
    """SwabianTimeTagger indexes trigger_levels by a channel number computed at
    runtime. Reported, never guessed -- and never silently dropped."""
    assert len(report.unresolved) == 3
    assert all(item.startswith("SwabianTimeTaggerManager: tl.get(str(self._") for item in report.unresolved)
    assert all("sub-key of 'trigger_levels'" in item for item in report.unresolved)


def test_no_writes_masquerade_as_reads(report):
    assert report.writes == 0


# ── the managers the exit criterion names ─────────────────────────────────
def test_aaaotf_guarded_reads_are_optional_and_nothing_is_constrained(extractions):
    props = extractions["AAAOTFLaserManager"].properties
    assert {"calibCsvPath", "channel", "frequencyMHz", "protocolProfile",
            "rs232device", "toggleTrueExternal", "ttlToggling"} <= set(props)
    for key in ("calibCsvPath", "ttlToggling", "toggleTrueExternal"):
        assert props[key].required == ex.OPTIONAL, key
    # Base-class reads come first: LaserManager.hasProperty("calibCsvPath") is
    # an inherited membership check; the manager's own read is the guarded one.
    assert any(read.guard and read.guard.startswith("try/except") for read in props["calibCsvPath"].reads)
    assert any(read.guard == "in" for read in props["ttlToggling"].reads)
    assert {k for k, p in props.items() if p.required == ex.REQUIRED} == {"channel", "rs232device"}
    assert props["rs232device"].ref_category == "rs232devices"
    # The code never wraps a read in Path()/int()/.items(): nothing is provable.
    assert all(p.constraint is None for p in props.values())
    assert props["ttlToggling"].kind == ex.KIND_BOOLEAN
    assert props["ttlToggling"].kind_source == ["docs:boolean"]
    assert props["protocolProfile"].nullable


def test_thorcam_camera_serial_is_nullable_and_typed_by_the_examples(extractions):
    spec = extractions["ThorCamTSIManager"].properties["cameraSerial"]
    assert spec.nullable is True
    assert spec.kind == ex.KIND_STRING
    assert spec.kind_source == ["example:string"]
    assert spec.constraint is None


def test_apd_alias_spellings_fold_into_their_camel_case_property(extractions):
    props = extractions["APDManager"].properties
    assert "mock_random_seed" not in props
    assert props["mockRandomSeed"].aliases == ("mock_random_seed",)
    assert props["mockPhotonCountMean"].aliases == ("mock_photon_count_mean",)
    assert props["mockPhotonCountMean"].kind == ex.KIND_NUMBER


def test_cobolt_inherits_its_reads_from_the_lantz_base(extractions):
    manager = extractions["Cobolt0601LaserManager"]
    assert manager.classes[0] == "Cobolt0601LaserManager"
    assert "LantzLaserManager" in manager.classes
    assert "digitalPorts" in manager.properties


def test_detectors_inherit_camera_pixel_size_through_a_module_function(extractions):
    """DetectorManager reads CAMERA_PIXEL_SIZE_KEY inside configuredCameraPixelSize(managerProperties)."""
    for name in ("HamamatsuManager", "ThorCamTSIManager", "APDManager"):
        spec = extractions[name].properties["cameraPixelSizeUm"]
        assert spec.nullable and spec.required == ex.OPTIONAL
        assert spec.reads[0].via == "function:configuredCameraPixelSize"


def test_thorlabs_mff_reads_through_its_key_helper(extractions):
    props = extractions["ThorlabsMFFManager"].properties
    assert {"serial_number", "invert", "initial_state", "state_names"} <= set(props)
    assert props["invert"].kind == ex.KIND_BOOLEAN
    assert all(read.via == "helper:_read_info" for read in props["serial_number"].reads)


def test_thorcam_defaults_carry_their_sub_keys(extractions):
    spec = extractions["ThorCamTSIManager"].properties["defaults"]
    assert spec.constraint == {"type": "object"}
    assert {"exposure_us", "gain", "operation_mode"} <= set(spec.sub_properties)


def test_pistage_usb_description_is_read_in_a_method_handed_the_dict(extractions):
    spec = extractions["PIStageManager"].properties["usb_description"]
    assert spec.reads[0].via == "method:_resolve_usb_description"


def test_rs232_manager_is_open_because_the_driver_takes_the_whole_dict():
    # Extracted from the tree directly: the catalog's legacy scan skips
    # ``RS232Manager`` as a base class, although shipped setups select it by
    # that name and a template exists for it. Noted in the plan as a catalog
    # gap; not this module's to fix.
    manager = ex.merge_manager("RS232Manager", ex.extract_tree(MANAGERS_ROOT))
    assert manager.open_passthrough is True
    assert {"port", "recv_termination"} <= set(manager.properties)


def test_hamamatsu_vendor_dict_is_an_object_with_no_sub_keys(extractions):
    props = extractions["HamamatsuManager"].properties
    assert props["hamamatsu"].constraint == {"type": "object"}
    assert not any(key.startswith("hamamatsu.") for key in props)


def test_camera_list_index_is_a_union_from_the_examples(extractions):
    """int in some shipped setups, "mock" in others -- so the widget is a text box."""
    spec = extractions["HamamatsuManager"].properties["cameraListIndex"]
    assert spec.kind == [ex.KIND_INTEGER, ex.KIND_STRING]
    assert spec.constraint is None, "a union from usage is never a constraint"


# ── the snapshot ──────────────────────────────────────────────────────────
def test_extraction_matches_the_checked_in_snapshot(report):
    """Regenerate with tools/extract_manager_schemas.py --report --json."""
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = report.snapshot()
    assert actual["totals"] == expected["totals"], (
        "coverage changed; if a manager legitimately gained or lost a property, "
        "regenerate: python tools/extract_manager_schemas.py --report --json "
        f"> {SNAPSHOT.relative_to(_REPO)}"
    )
    moved = [
        (a["name"], e, a)
        for a, e in zip(actual["managers"], expected["managers"])
        if a != e
    ]
    assert not moved, f"per-manager coverage moved: {moved[:5]}"


# ── the tool never imports the manager stack ──────────────────────────────
def test_the_tool_runs_with_manager_and_qt_imports_forbidden(tmp_path):
    """A fresh process where importing any manager module or Qt raises.

    ``imswitch/imcontrol/model/__init__.py`` imports both; the tool installs a
    bare package object in its place. If that ever regresses, this fails on
    the first forbidden import rather than passing by accident.
    """
    import os
    import subprocess
    import sys

    guard = tmp_path / "forbid_imports.py"
    guard.write_text(textwrap.dedent('''
        import importlib.abc, sys
        FORBIDDEN = ("imswitch.imcontrol.model.managers", "qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6")
        class Forbid(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN):
                    raise ImportError(f"forbidden import in the extraction tool: {name}")
                return None
        sys.meta_path.insert(0, Forbid())
        sys.argv = ["extract_manager_schemas.py", "--report", "--json"]
        import runpy
        runpy.run_path(sys.argv_tool, run_name="__main__")
    ''').replace("sys.argv_tool", repr(str(TOOL))), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(_REPO), "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run([sys.executable, str(guard)], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "imswitch.imcontrol.model.managers" not in result.stderr
    totals = json.loads(result.stdout)["totals"]
    assert totals["managers"] == 61


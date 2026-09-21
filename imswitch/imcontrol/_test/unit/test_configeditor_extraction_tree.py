"""Extraction over the real manager tree: the Phase 0 exit criterion.

The rule tests pin what the extractor may conclude from a snippet. These pin
what it concludes from ImSwitch itself -- the numbers the plan quotes, the
managers it names, and a checked-in snapshot that fails the moment a manager
reads a new key without the schemas being regenerated (Phase 1 turns that
into the drift guard; here it already tells you *which* manager moved).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import extraction as ex
from imswitch.imcontrol.model.configeditor.catalog import build_catalog

_REPO = Path(__file__).resolve().parents[4]
MANAGERS_ROOT = _REPO / "imswitch" / "imcontrol" / "model" / "managers"
SETUPS_DIR = _REPO / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
DOCS_DIR = _REPO / "docs" / "devices"
SNAPSHOT = Path(__file__).with_name("configeditor_extraction_snapshot.json")


@pytest.fixture(scope="module")
def extractions():
    names = sorted(info.manager_name for info in build_catalog().managers())
    return ex.extract_managers(
        names, managers_root=MANAGERS_ROOT, setups_dir=SETUPS_DIR, docs_dir=DOCS_DIR
    )


@pytest.fixture(scope="module")
def report(extractions):
    return ex.coverage_report(extractions, docs=ex.docs_cards(DOCS_DIR))


# ── the numbers the plan quotes ───────────────────────────────────────────
def test_the_coverage_the_plan_is_built_on(report):
    assert report.managers == 64
    assert report.reads_any == 54, "PulseGeneratorLaserManager only mentions the word in a docstring"
    assert report.with_keys == 54
    # 188 spellings in the probe; nine of them are APD/PMT snake_case aliases
    # of camelCase properties and fold into one property each.
    assert report.keys + report.alias_spellings == 188
    assert report.alias_spellings == 9
    assert report.required == 68
    assert report.optional == 111
    assert report.refs == 14
    assert report.none_default_only == 13
    assert (report.docs_agree, report.docs_documented) == (111, 118)


def test_kinds_come_from_code_then_examples_then_docs(report):
    assert report.typed_by_code == 88
    assert report.typed_with_examples > report.typed_by_code
    assert report.typed_with_docs > report.typed_with_examples
    assert report.typed_with_docs <= report.keys


def test_no_selectable_manager_reads_another_devices_properties(report):
    """The strict receiver rule discards only TriggerScopeManager, which is not selectable."""
    assert report.discarded == []


def test_the_tree_has_no_uncertain_requiredness_today(report):
    """Not a rule -- a fact about the tree, so a future one is noticed."""
    assert report.uncertain_keys == []


# ── the managers the exit criterion names ─────────────────────────────────
def test_aaaotf_guarded_reads_are_optional_and_nothing_is_constrained(extractions):
    props = extractions["AAAOTFLaserManager"].properties
    assert set(props) == {"calibCsvPath", "channel", "frequencyMHz", "protocolProfile",
                          "rs232device", "toggleTrueExternal", "ttlToggling"}
    for key in ("calibCsvPath", "ttlToggling", "toggleTrueExternal"):
        assert props[key].required == ex.OPTIONAL, key
    assert props["calibCsvPath"].reads[0].guard.startswith("try/except")
    assert props["ttlToggling"].reads[0].guard == "in"
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


def test_cobolt_inherits_its_only_read_from_the_lantz_base(extractions):
    manager = extractions["Cobolt0601LaserManager"]
    assert manager.classes[0] == "Cobolt0601LaserManager"
    assert "LantzLaserManager" in manager.classes
    assert set(manager.properties) == {"digitalPorts"}


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

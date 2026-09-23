"""The checked-in schemas are what the source tree says, or CI fails.

Regenerates every schema, fixture and the index in memory exactly as
``tools/extract_manager_schemas.py --write`` would, and compares with what is
checked in -- the pattern ``test_communication_channel_contract.py`` uses for
its signal inventory. Also: every schema is a valid Draft 2020-12 schema,
every fixture satisfies its schema, and the two seed overrides do what the
plan says they are for.

``jsonschema`` is a test dependency (the ``test`` extra), not a runtime one:
these tests assert it is present rather than skipping, so validation cannot
quietly stop running in CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import schemagen as sg

_REPO = Path(__file__).resolve().parents[4]
_TOOL = _REPO / "tools" / "extract_manager_schemas.py"
SCHEMAS_ROOT = sg.schemas_root()


def _tool():
    spec = importlib.util.spec_from_file_location("extract_manager_schemas", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _tool()


@pytest.fixture(scope="module")
def inputs(tool):
    args = argparse.Namespace(
        managers_root=tool.DEFAULT_MANAGERS_ROOT, setups_dir=tool.DEFAULT_SETUPS_DIR,
        docs_dir=tool.DEFAULT_DOCS_DIR, templates_dir=tool.DEFAULT_TEMPLATES_DIR,
        schemas_root=SCHEMAS_ROOT, no_examples=False, no_docs=False,
        setup_info=tool.DEFAULT_SETUP_INFO,
    )
    return tool.generation_inputs(args)


@pytest.fixture(scope="module")
def generated(inputs):
    return sg.generate_all(inputs)


@pytest.fixture(scope="module")
def jsonschema():
    try:
        import jsonschema as module
    except ImportError:  # pragma: no cover - the point is that CI never gets here
        pytest.fail("jsonschema is missing: it is in the `test` extra (pip install -e '.[test]')")
    return module


# ── the drift guard ───────────────────────────────────────────────────────
def test_checked_in_schemas_match_the_source_tree(generated):
    problems = sg.check(generated, SCHEMAS_ROOT)
    assert problems == [], (
        "generated schemas differ from the source tree:\n  " + "\n  ".join(problems)
        + f"\nRegenerate with: {sg.REGENERATE_COMMAND}"
    )


def test_the_generation_set_is_the_catalog_plus_template_backed_managers(inputs):
    names = set(inputs.extractions)
    assert {"RS232Manager", "PiezoconceptZManager2"} <= names, "template-backed (RS232Manager is in the catalog too now)"
    assert {"AAAOTFLaserManager", "HamamatsuManager", "MockPositionerManager"} <= names
    # The two mock contributions resolve through their python_name; the one
    # name left is a vendor driver module the legacy scan mistook for a manager.
    assert set(inputs.unresolved) == {"PyCoboltManager"}
    # 61 in the catalog, minus that one, plus PiezoconceptZManager2, which only
    # a template names. (65 until the four camera managers whose drivers were never in the tree (Basler, ESP32Cam, GXPIPY, JetsonCam) were removed by the magic-number audit.)
    assert len(names) == 61


def test_index_names_every_manager_and_its_override_status(generated):
    index = json.loads(generated["index.json"])
    assert set(index["managers"]) == {p.removeprefix("managers/").removesuffix(".json")
                                      for p in generated if p.startswith("managers/")}
    assert index["managers"]["AAAOTFLaserManager"]["overridden"] is True
    assert index["managers"]["HamamatsuManager"]["overridden"] is True
    assert index["managers"]["NidaqLaserManager"]["overridden"] is False
    # 211: the removed camera managers took their keys with them.
    assert index["coverage"]["keys"] >= 211


# ── validity ──────────────────────────────────────────────────────────────
def test_every_schema_is_a_valid_draft_2020_12_schema(generated, jsonschema):
    for path, text in generated.items():
        if path.startswith("managers/"):
            jsonschema.Draft202012Validator.check_schema(json.loads(text))


def test_every_fixture_satisfies_its_own_schema(generated, jsonschema):
    for path, text in generated.items():
        if not path.startswith("fixtures/"):
            continue
        name = path.removeprefix("fixtures/").removesuffix(".json")
        schema = json.loads(generated[f"managers/{name}.json"])
        fixture = json.loads(text)
        assert fixture["device"]["managerName"] == name
        jsonschema.Draft202012Validator(schema).validate(fixture["device"]["managerProperties"])


def test_every_fixture_names_a_real_setup_section(generated):
    from imswitch.imcontrol.model.plugins.setup_metadata import CATEGORY_METADATA
    for path, text in generated.items():
        if path.startswith("fixtures/"):
            assert json.loads(text)["category"] in CATEGORY_METADATA, path


# ── the seed overrides ────────────────────────────────────────────────────
def test_aaaotf_protocol_profile_is_the_closed_set_the_manager_accepts(generated, jsonschema):
    schema = json.loads(generated["managers/AAAOTFLaserManager.json"])
    prop = schema["properties"]["protocolProfile"]
    assert prop["enum"] == ["aa.compatibility", "aa.frequency-startup", "", None]
    assert "override" in prop["x-imswitch-source"]
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid({"rs232device": "aotf", "channel": 1, "protocolProfile": "aa.compatibility"})
    assert not validator.is_valid({"rs232device": "aotf", "channel": 1, "protocolProfile": "aa.nope"})


def test_hamamatsu_camera_list_index_narrowing_comes_from_the_override(generated, jsonschema):
    """The rules alone give int-or-string from usage and no constraint; the
    override is what makes an object invalid -- and "x" stays valid."""
    schema = json.loads(generated["managers/HamamatsuManager.json"])
    prop = schema["properties"]["cameraListIndex"]
    assert prop["type"] == ["integer", "string"]
    assert prop["x-imswitch-kind"] == ["integer", "string"]
    validator = jsonschema.Draft202012Validator(schema)
    assert not validator.is_valid({"cameraListIndex": {"a": 1}, "hamamatsu": {}})
    assert validator.is_valid({"cameraListIndex": "x", "hamamatsu": {}})


def test_generated_schemas_never_narrow_without_an_override(generated):
    """A validation type appears only with a provable source or an override."""
    # ``code:sub-keys``: the manager reads named keys inside the dict
    # (``defaults.get("exposure_us")``), which proves it is a mapping.
    provable = {"code:Path()", "code:open()", "code:int()", "code:float()",
                "code:mapping-use", "code:sub-keys", "override"}
    for path, text in generated.items():
        if not path.startswith("managers/"):
            continue
        for key, prop in json.loads(text)["properties"].items():
            if "type" in prop:
                assert provable & set(prop["x-imswitch-source"]), f"{path}:{key} has a type with no proof"

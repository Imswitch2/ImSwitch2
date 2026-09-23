"""Phase 5: a template is a presentation overlay and cannot drift from the code.

A template may name only keys the code reads: the manager's schema (either
spelling), a role that applies to the category, or -- for a manager that
hands its whole dict to a driver -- anything. A template type is either
absent or a compatible refinement of the schema's kind; a type that merely
repeats the kind is stripped, so the schema alone says what a value is.
Would have caught APD/PMT's undeclared keys, SwabianTimeTagger's runtime
parameter listed as a property, and AAAOTF's missing ``ttlToggling``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import resources, roles as roles_module
from imswitch.imcontrol.model.configeditor.kinds import NOT_FORM_FIELDS
from imswitch.imcontrol.model.configeditor.schemas import _infer_type_from_schema, materialize_device_schema, resolve_type
from imswitch.imcontrol.model.plugins.setup_metadata import CATEGORY_METADATA

TEMPLATES = Path(__file__).resolve().parents[4] / "imswitch" / "imcontrol" / "view" / "configeditor" / "builtin_templates"


def _templates() -> list[tuple[str, str, dict]]:
    found = []
    for path in sorted(TEMPLATES.glob("*/*.json")):
        if path.parent.name == "sections" or path.stem.startswith("_"):
            continue
        found.append((path.parent.name, path.stem, json.loads(path.read_text(encoding="utf-8"))))
    return found


TEMPLATE_CASES = _templates()
IDS = [f"{category}/{name}" for category, name, _ in TEMPLATE_CASES]


def _role_keys(category: str) -> set[str]:
    return {
        key for role in resources.roles() if category in roles_module.sections_of(role)
        for key in (role.get("properties") or {})
    }


@pytest.mark.parametrize("category, name, template", TEMPLATE_CASES, ids=IDS)
def test_every_template_key_is_one_the_code_reads(category, name, template):
    schema = resources.generated_schema_for(name)
    assert schema is not None, f"{name}: a template for a manager with no schema"
    assert template.get("category") == category
    props = schema.get("properties") or {}
    allowed = set(props) | _role_keys(category)
    if not schema.get("x-imswitch-open-passthrough"):
        unknown = [f["key"] for f in template.get("props", []) if f["key"] not in allowed]
        assert unknown == [], f"{name}: template lists properties the code never reads: {unknown}"
        for nest_key in template.get("nested", {}):
            assert nest_key in props and props[nest_key].get("x-imswitch-kind") == "object", \
                f"{name}: nested section {nest_key!r} is not an object property of the schema"
    kind = CATEGORY_METADATA[category].kind
    kind_props = (resources.kind_schema_for(kind) or {}).get("properties") or {}
    unknown_top = [f["key"] for f in template.get("top", []) if f["key"] not in kind_props or f["key"] in NOT_FORM_FIELDS]
    assert unknown_top == [], f"{name}: top-level keys the {kind} dataclass does not declare: {unknown_top}"


@pytest.mark.parametrize("category, name, template", TEMPLATE_CASES, ids=IDS)
def test_every_template_type_is_a_refinement_and_never_a_repeat(category, name, template):
    schema = resources.generated_schema_for(name) or {}
    props = schema.get("properties") or {}
    kind_props = (resources.kind_schema_for(CATEGORY_METADATA[category].kind) or {}).get("properties") or {}
    for section, fields, described in (("top", template.get("top", []), kind_props),
                                        ("props", template.get("props", []), props)):
        for f in fields:
            tp = f.get("type")
            prop = described.get(f["key"])
            if tp is None or prop is None:
                continue
            assert resolve_type(tp, prop) == tp, \
                f"{name}.{section}.{f['key']}: template type {tp!r} contradicts the schema ({_infer_type_from_schema(prop)!r})"
            assert _infer_type_from_schema(prop) != tp, \
                f"{name}.{section}.{f['key']}: template type {tp!r} merely repeats the schema; strip it"


@pytest.mark.parametrize("category, name, template", TEMPLATE_CASES, ids=IDS)
def test_the_form_still_types_every_template_field(category, name, template):
    """With types stripped, the schema and the kind supply them."""
    form = materialize_device_schema(
        template=template, json_schema=resources.generated_schema_for(name),
        kind_schema=resources.kind_schema_for(CATEGORY_METADATA[category].kind),
    )
    for section in ("top", "props"):
        for f in form[section]:
            assert f.get("type"), f"{name}.{section}.{f['key']} has no type"
    for nest_fields in form["nested"].values():
        for f in nest_fields:
            assert f.get("type"), f"{name}.nested.{f['key']} has no type"


def test_the_overlay_is_mostly_presentation_now():
    """Most template fields carry no type at all; the ones that do refine."""
    typed = sum(1 for _c, _n, t in TEMPLATE_CASES for f in t.get("top", []) + t.get("props", []) if "type" in f)
    total = sum(len(t.get("top", [])) + len(t.get("props", [])) for _c, _n, t in TEMPLATE_CASES)
    assert total >= 180
    assert typed <= total * 0.4, f"{typed} of {total} template fields still carry a type"


def test_the_stripped_drift_stays_gone():
    swabian = json.loads((TEMPLATES / "detectors" / "SwabianTimeTaggerManager.json").read_text(encoding="utf-8"))
    assert "accumulate_mode" not in {f["key"] for f in swabian["props"]}, "a runtime parameter, not a property"
    hamamatsu = json.loads((TEMPLATES / "detectors" / "HamamatsuManager.json").read_text(encoding="utf-8"))
    [index] = [f for f in hamamatsu["props"] if f["key"] == "cameraListIndex"]
    assert "type" not in index, "the schema's union decides: a text box that holds 0 and \"mock\""
    aotf = json.loads((TEMPLATES / "lasers" / "AAAOTFLaserManager.json").read_text(encoding="utf-8"))
    [device] = [f for f in aotf["props"] if f["key"] == "rs232device"]
    assert "type" not in device, "the schema's ref widget decides"

"""Shared helpers for the config editor's round-trip tests.

``assert_json_identical`` is the comparison the schema-extraction plan's
acceptance criterion asks for: Python's ``==`` says ``1 == 1.0 == True`` and
``{} != None`` only by luck of type, so a round trip that turned an int into
a float, a bool into an int, or a null into an empty dict would pass a plain
equality assertion. This one compares JSON *kinds* first, then values, and
names the path that differs.

``corpus()`` is the plan's corpus: the shipped setups, the generated
per-manager fixtures, and the hand-written value-shape fixtures beside this
module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

_REPO = Path(__file__).resolve().parents[4]
SHIPPED_SETUPS = _REPO / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
GENERATED_FIXTURES = _REPO / "imswitch" / "imcontrol" / "model" / "configeditor" / "schemas" / "fixtures"
VALUE_SHAPE_FIXTURES = Path(__file__).with_name("configeditor_fixtures")


def json_kind(value) -> str:
    """The JSON kind of a Python value; ``bool`` before ``int``, as JSON has it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def assert_json_identical(actual, expected, path: str = "$") -> None:
    """Fail unless ``actual`` and ``expected`` are the same JSON document, kind for kind."""
    actual_kind, expected_kind = json_kind(actual), json_kind(expected)
    if actual_kind != expected_kind:
        raise AssertionError(
            f"{path}: expected {expected_kind} {expected!r}, got {actual_kind} {actual!r}")
    if expected_kind == "object":
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        if missing or unexpected:
            raise AssertionError(f"{path}: keys differ; missing {missing}, unexpected {unexpected}")
        for key in expected:
            assert_json_identical(actual[key], expected[key], f"{path}.{key}")
    elif expected_kind == "array":
        if len(actual) != len(expected):
            raise AssertionError(f"{path}: expected {len(expected)} items, got {len(actual)}")
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            assert_json_identical(item, wanted, f"{path}[{index}]")
    elif actual != expected:
        raise AssertionError(f"{path}: expected {expected!r}, got {actual!r}")


def devices_in(setup: dict) -> Iterator[tuple[str, str, dict]]:
    """``(section, name, device)`` for every device entry of a setup document."""
    for section, entries in setup.items():
        if not isinstance(entries, dict):
            continue
        for name, device in entries.items():
            if isinstance(device, dict) and "managerName" in device:
                yield section, name, device


def shipped_setups() -> dict[str, dict]:
    return {path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(SHIPPED_SETUPS.glob("*.json"))}


def value_shape_fixtures() -> dict[str, dict]:
    """``file name -> setup`` for the hand-written value-shape fixtures."""
    found = {}
    for path in sorted(VALUE_SHAPE_FIXTURES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert set(document) == {"why", "setup"}, f"{path.name}: expected {{why, setup}}"
        found[path.name] = document["setup"]
    return found


def generated_fixtures() -> dict[str, dict]:
    """``manager name -> setup`` with the one synthetic device the generator wrote."""
    found = {}
    for path in sorted(GENERATED_FIXTURES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        found[path.stem] = {document["category"]: {path.stem: document["device"]}}
    return found


def corpus() -> list[tuple[str, str, str, dict]]:
    """``(source, section, name, device)`` over the whole round-trip corpus."""
    items = []
    for label, setups in (("shipped", shipped_setups()),
                          ("generated", generated_fixtures()),
                          ("value-shape", value_shape_fixtures())):
        for file_name, setup in setups.items():
            for section, name, device in devices_in(setup):
                items.append((f"{label}:{file_name}", section, name, device))
    return items

"""Every System section template says what ``SetupInfo`` says.

A section template (``builtin_templates/sections/<key>.json``) is what the
editor offers for a block of ``SetupInfo``. It may not drift from the
dataclass that reads the block:

* it lists every field of the dataclass and nothing else (a missing field is
  one the operator cannot set, and one an Edit -> Apply used to drop);
* a field is ``req`` exactly when the loader needs it;
* a required field starts EMPTY -- a number the dataclass does not default is
  one the operator must choose, not one the editor may guess;
* an optional field starts at the dataclass default, so showing it and
  applying it untouched does not change what the code does.

"Needs it" is measured, not read off ``dataclasses.MISSING``: the setup file
is loaded by ``SetupInfo.from_json(..., infer_missing=True)`` (that is what
``configfiletools.loadSetupInfo`` does), which turns a missing key into
``None`` and warns instead of failing, and treats an ``Optional`` field
without a default as optional. A key is *required* here if dropping it from a
valid block makes that loader raise (``focusLock.updateFreq``, through
``__post_init__``) or warn that a non-optional value was defaulted to None.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import typing
import warnings
from pathlib import Path

import pytest

# ``imswitch.imcontrol.model`` re-exports the class under the module's own
# name, so the module itself has to be imported by path.
setup_info = importlib.import_module("imswitch.imcontrol.model.SetupInfo")
SetupInfo = setup_info.SetupInfo

SECTIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "view" / "configeditor" / "builtin_templates" / "sections"
)

#: Section key -> the dataclass the loader builds for it. ``processing`` has
#: none (ImProcess reads its own block) and is pinned by its own tests.
SECTION_CLASSES = {
    "focusLock": setup_info.FocusLockInfo,
    "autofocus": setup_info.AutofocusInfo,
    "tiling": setup_info.TilingInfo,
    "scan": setup_info.ScanInfo,
    "microscopeStand": setup_info.MicroscopeStandInfo,
    "etSTED": setup_info.EtSTEDInfo,
    "nidaq": setup_info.NidaqInfo,
    "pulseStreamer": setup_info.PulseStreamerInfo,
    "teensyPulse": setup_info.TeensyPulseInfo,
    "pyroServerInfo": setup_info.PyroServerInfo,
    "triggerScope": setup_info.TriggerScopeInfo,
}

#: Fields the dataclass reads that the template does not offer yet.
#: Added in S1 of the section-templates work; the set is emptied there.
EXPECTED_MISSING_FROM_TEMPLATE = {
    "focusLock": {"positionerAxis", "reacquireTimeoutS", "reacquireTolerancePx", "reacquireSamples"},
    "autofocus": {"settleTimeMs"},
    "tiling": {
        "mode", "scanSource", "scanTimeoutS", "settleTimeMs", "saveTiles", "saveFormat",
        "measurementsRoot", "flipTileAxisX", "flipTileAxisY", "swapTileAxes",
        "registerTiles", "registrationMaxShiftFraction",
    },
}
#: Sections that have no template yet (added in S1).
EXPECTED_NO_TEMPLATE = {"triggerScope"}

#: Optional fields whose template default is known to differ from the
#: dataclass default (fixed in S1).
EXPECTED_DEFAULT_MISMATCH = {("teensyPulse", "useMockOnFailure")}


def _template(key: str) -> dict:
    path = SECTIONS_DIR / f"{key}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _templates_present():
    return [k for k in SECTION_CLASSES if (SECTIONS_DIR / f"{k}.json").is_file()]


def _fields(key: str) -> dict:
    return {f["key"]: f for f in _template(key)["fields"]}


# ---------------------------------------------------------------- ground truth

def _dummy(tp):
    origin = typing.get_origin(tp)
    if origin is typing.Union:
        inner = [a for a in typing.get_args(tp) if a is not type(None)]
        return _dummy(inner[0])
    if tp is bool:
        return True
    if tp is int:
        return 5
    if tp is float:
        return 1.5
    if tp is str:
        return "x"
    if tp is dict or origin is dict:
        return {}
    return "x"


def _default_of(f: dataclasses.Field):
    """``(has_default, value)`` -- the default the dataclass itself states."""
    if f.default is not dataclasses.MISSING:
        return True, f.default
    if f.default_factory is not dataclasses.MISSING:
        return True, f.default_factory()
    return False, None


def valid_block(cls) -> dict:
    """A block the loader accepts: every field, its default or a type-correct dummy."""
    hints = typing.get_type_hints(cls)
    out = {}
    for f in dataclasses.fields(cls):
        has, value = _default_of(f)
        out[f.name] = value if has else _dummy(hints[f.name])
    return out


def _load(key: str, block: dict) -> list[str]:
    """Load ``{key: block}`` as ``loadSetupInfo`` does; return the warnings it raised."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        SetupInfo.from_json(json.dumps({key: block}), infer_missing=True)
    return [str(w.message) for w in caught]


def loader_required(key: str, cls) -> set[str]:
    block = valid_block(cls)
    _load(key, block)  # the reference block must load cleanly
    required = set()
    for f in dataclasses.fields(cls):
        trimmed = {k: v for k, v in block.items() if k != f.name}
        try:
            messages = _load(key, trimmed)
        except Exception:
            required.add(f.name)
            continue
        if any(f"non-optional type {f.name} detected" in m for m in messages):
            required.add(f.name)
    return required


def declared_without_default(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls) if not _default_of(f)[0]}


def loader_default(f: dataclasses.Field):
    """What the loader gives a missing optional key: the stated default, else None."""
    has, value = _default_of(f)
    return value if has else None


# ------------------------------------------------------------------ the tests

def test_there_are_templates_to_check():
    assert len(_templates_present()) >= 10


def test_every_section_dataclass_is_mapped_to_its_setupinfo_field():
    """The mapping above is the loader's own: SetupInfo types each key by that class."""
    hints = typing.get_type_hints(SetupInfo)
    for key, cls in SECTION_CLASSES.items():
        hint = hints[key]
        inner = [a for a in typing.get_args(hint) if a is not type(None)] or [hint]
        assert inner[0] is cls, key


def test_the_set_of_templates_is_the_known_one():
    on_disk = {p.stem for p in SECTIONS_DIR.glob("*.json")}
    assert on_disk - {"processing"} == set(SECTION_CLASSES) - EXPECTED_NO_TEMPLATE


@pytest.mark.parametrize("key", _templates_present())
def test_template_lists_every_dataclass_field_and_nothing_else(key):
    cls = SECTION_CLASSES[key]
    in_code = {f.name for f in dataclasses.fields(cls)}
    in_template = set(_fields(key))
    expected_missing = EXPECTED_MISSING_FROM_TEMPLATE.get(key, set())
    assert in_template - in_code == set(), f"{key}: template lists keys the code never reads"
    assert in_code - in_template == expected_missing, (
        f"{key}: template does not offer {sorted(in_code - in_template - expected_missing)}"
    )


@pytest.mark.parametrize("key", _templates_present())
def test_template_req_is_what_the_loader_requires(key):
    cls = SECTION_CLASSES[key]
    truth = loader_required(key, cls)
    fields = _fields(key)
    flagged = {k for k, f in fields.items() if f.get("req")}
    assert flagged == truth & set(fields), (
        f"{key}: req differs from the loader -- template-only {sorted(flagged - truth)}, "
        f"loader-only {sorted((truth & set(fields)) - flagged)}"
    )


def test_where_the_loader_and_dataclasses_missing_differ_is_known():
    """``Optional`` without a default is optional to the loader, required to ``MISSING``."""
    differences = {}
    for key, cls in SECTION_CLASSES.items():
        diff = declared_without_default(cls) ^ loader_required(key, cls)
        if diff:
            differences[key] = diff
    assert differences == {"microscopeStand": {"managerProperties"}}


@pytest.mark.parametrize("key", _templates_present())
def test_required_fields_start_empty_and_read_as_missing(key):
    from imswitch.imcontrol.view.configeditor import editor

    schema = editor.SECTION_SCHEMAS[key]
    seeded = editor._build_default_section(schema)
    for k, f in _fields(key).items():
        if f.get("req"):
            assert seeded[k] in (None, "", []), f"{key}.{k} starts as {seeded[k]!r}, which reads as filled in"


def _normalised(value):
    return None if value == "null" else value


@pytest.mark.parametrize("key", _templates_present())
def test_optional_defaults_are_the_dataclass_defaults(key):
    from imswitch.imcontrol.view.configeditor import editor

    cls = SECTION_CLASSES[key]
    by_name = {f.name: f for f in dataclasses.fields(cls)}
    for k, f in _fields(key).items():
        if f.get("req"):
            continue
        if (key, k) in EXPECTED_DEFAULT_MISMATCH:
            continue
        shown = _normalised(editor._field_default(f))
        assert shown == loader_default(by_name[k]), (
            f"{key}.{k}: template starts at {shown!r}, the code at {loader_default(by_name[k])!r}"
        )


def test_the_known_default_mismatches_are_still_mismatches():
    """Keeps EXPECTED_DEFAULT_MISMATCH honest: an entry that no longer differs must go."""
    from imswitch.imcontrol.view.configeditor import editor

    for key, k in EXPECTED_DEFAULT_MISMATCH:
        by_name = {f.name: f for f in dataclasses.fields(SECTION_CLASSES[key])}
        shown = _normalised(editor._field_default(_fields(key)[k]))
        assert shown != loader_default(by_name[k])


def test_the_expected_missing_fields_really_are_missing():
    """Keeps EXPECTED_MISSING_FROM_TEMPLATE honest: a field offered since must leave the set."""
    for key, names in EXPECTED_MISSING_FROM_TEMPLATE.items():
        assert names.isdisjoint(_fields(key)), key
        assert names <= {f.name for f in dataclasses.fields(SECTION_CLASSES[key])}, key


@pytest.mark.parametrize("key", _templates_present())
def test_every_template_is_a_section_that_writes_as_written(key):
    from imswitch.imcontrol.view.configeditor import editor

    schema = editor.SECTION_SCHEMAS[key]
    assert schema["section"] is True
    assert schema["key"] == key
    assert editor._writes_as_written(schema)

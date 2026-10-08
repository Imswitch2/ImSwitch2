"""The config editor's Time Tagger section offers exactly the block's fields.

``TimeTaggerInfo`` is the block; the section template is how the editor
shows it. A field in one and not the other is either invisible (settable
only by hand-editing the JSON) or a lie (offered, never read), so the two
are compared rather than remembered. The section's defaults must also be
the dataclass's, or a new setup made in the editor differs from one made
by hand.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.configeditor.defaults import _default_value
from imswitch.imcontrol.model.managers.TimeTaggerManager import ROLE_FIELDS

pytestmark = pytest.mark.nohardware

_SECTION = (Path(__file__).resolve().parents[2] / "view" / "configeditor"
            / "builtin_templates" / "sections" / "timeTagger.json")


def _section():
    return json.loads(_SECTION.read_text(encoding="utf-8"))


def test_section_is_registered_as_a_system_section():
    section = _section()
    assert section["section"] is True
    assert section["key"] == "timeTagger"
    assert section["group"] == "system"


def test_section_fields_are_exactly_the_dataclass_fields():
    offered = [f["key"] for f in _section()["fields"]]
    declared = [f.name for f in dataclasses.fields(TimeTaggerInfo)]
    assert sorted(offered) == sorted(declared)
    assert len(offered) == len(set(offered)), "duplicate field in the section"


def test_section_defaults_match_the_dataclass_defaults():
    expected = {f.name: f.default for f in dataclasses.fields(TimeTaggerInfo)}
    for field in _section()["fields"]:
        value = _default_value(field.get("default", ""), field.get("type", "text"))
        assert value == expected[field["key"]], field["key"]


def test_every_role_the_manager_knows_has_its_fields_in_the_block():
    declared = {f.name for f in dataclasses.fields(TimeTaggerInfo)}
    for role, fields in ROLE_FIELDS.items():
        for name in fields:
            if name is not None:
                assert name in declared, f"{role}: {name}"

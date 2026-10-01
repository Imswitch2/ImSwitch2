"""The config editor's ImProcess section must expose every processing flag.

A flag that ImProcess reads but the editor does not offer is invisible: the
only way to set it is to hand-edit the setup JSON, which is exactly what the
editor exists to avoid. That gap opened twice without anyone noticing — once
for the metadata panel and once for the napari-storm viewer — so it is checked
rather than remembered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_CONFIG_MODULE = _REPO / "imswitch" / "improcess" / "model" / "processing_config.py"
_SECTION = (_REPO / "imswitch" / "imcontrol" / "view" / "configeditor"
            / "builtin_templates" / "sections" / "processing.json")

#: Keys the editor offers that are not simple flags — plugin id lists, which
#: are read through their own helper rather than ``processing_config.get``.
_NON_FLAG_FIELDS = {"reconstructors", "processors"}


def _flags_read_by_improcess() -> set[str]:
    source = _CONFIG_MODULE.read_text(encoding="utf-8")
    return set(re.findall(r'processing_config\.get\(\s*"([A-Za-z0-9_]+)"', source))


def _fields_offered_by_editor() -> dict[str, dict]:
    section = json.loads(_SECTION.read_text(encoding="utf-8"))
    return {field["key"]: field for field in section["fields"]}


def test_every_processing_flag_is_editable():
    missing = sorted(_flags_read_by_improcess() - set(_fields_offered_by_editor()))
    assert not missing, (
        f"processing flags read by ImProcess but absent from the config "
        f"editor's ImProcess section: {missing}. Add them to "
        f"{_SECTION.relative_to(_REPO)} so they "
        f"can be set without hand-editing the setup JSON."
    )


def test_the_editor_offers_no_flag_improcess_ignores():
    """The other direction: a control that silently does nothing is worse."""
    offered = set(_fields_offered_by_editor()) - _NON_FLAG_FIELDS
    unread = sorted(offered - _flags_read_by_improcess())
    assert not unread, (
        f"config editor offers processing settings ImProcess never reads: "
        f"{unread}"
    )


def test_the_napari_storm_flag_says_it_needs_the_optional_extra():
    """Turning it on with no package installed does nothing; say so up front."""
    field = _fields_offered_by_editor()["napariStormViewer"]
    assert field["type"] == "bool"
    assert field["default"] is False
    assert "storm" in field["tip"], "the tooltip must name the extra to install"


def test_processing_flags_all_default_to_booleans():
    for key, field in _fields_offered_by_editor().items():
        if key in _NON_FLAG_FIELDS:
            continue
        assert field["type"] == "bool", f"{key} should be a checkbox"
        assert isinstance(field["default"], bool), f"{key} has a non-bool default"

"""Phase 5: a manager cannot gain a property without ``docs/devices`` noticing.

Decision 3 of the schema-extraction plan: the device cards are hand-written
and drift-tested, not generated. For every manager with a card, every
property its schema lists appears in the card (or in the page's intro, for
a property every manager on that page inherits), and every card row names a
property the schema knows -- a sub-key of a nested object included. The
managers without a card are pinned by name, so adding a manager without
documenting it is noticed too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor import extraction as ex, resources

DOCS = Path(__file__).resolve().parents[4] / "docs" / "devices"

#: Properties every manager on a page inherits from its base class, documented
#: once in the page's introduction rather than on every card.
PAGE_WIDE = {
    "detectors": {"cameraPixelSizeUm"},
    "lasers": {"calibCsvPath"},
}

#: Managers with a schema and no card yet. Documenting one removes it here.
UNDOCUMENTED = {
    "ESP32LEDMatrixManager", "ESP32LightSheetManager", "ESP32Manager", "ESP32StageManager",
    "ElliptecManager", "GRBLLaserManager", "GRBLManager", "GRBLStageManager",
    "HamamatsuSLMdviManager", "HamamatsuSLMusbManager", "KDC101Manager", "LeicaDMIStandMockManager",
    "OxxiusCombinerLaserManager", "OxxiusLaserManager", "PulseStreamerManager", "RS232Manager",
    "SQUIDManager", "SerialDacZManager", "TeensyPulseManager", "ThorlabsMFFManager",
    "ThorlabsMFFMockManager", "TriggerScopeLaserManager", "TriggerScopePositionerManager",
}


@pytest.fixture(scope="module")
def cards():
    return ex.docs_cards(DOCS)


@pytest.fixture(scope="module")
def page_of():
    found = {}
    for path in sorted(DOCS.glob("*.rst")):
        for line in path.read_text(encoding="utf-8").splitlines():
            heading = ex._CARD_HEADING.match(line)
            if heading:
                found[heading.group(1)] = path.stem
    return found


def _schema_keys(schema: dict) -> tuple[set[str], set[str]]:
    """Canonical property keys, and the sub-keys of nested object properties."""
    props = schema.get("properties") or {}
    canonical = {key for key, prop in props.items() if "x-imswitch-alias-of" not in prop}
    nested = {sub for prop in props.values() for sub in (prop.get("properties") or {})}
    return canonical, nested


def test_page_wide_properties_are_really_in_the_page_intro():
    for page, keys in PAGE_WIDE.items():
        lines = (DOCS / f"{page}.rst").read_text(encoding="utf-8").splitlines()
        first_card = next(i for i, line in enumerate(lines) if ex._CARD_HEADING.match(line))
        intro = "\n".join(lines[:first_card])
        for key in keys:
            assert f"``{key}``" in intro, f"{page}.rst intro must document {key}"


def test_every_documented_manager_lists_every_property(cards, page_of):
    index = resources.index()
    missing = {}
    for name in index["managers"]:
        if name not in cards:
            continue
        canonical, _nested = _schema_keys(resources.generated_schema_for(name))
        allowed = set(cards[name]) | PAGE_WIDE.get(page_of.get(name, ""), set())
        gap = sorted(canonical - allowed)
        if gap:
            missing[name] = gap
    assert missing == {}, f"schema properties absent from their card: {missing}"


def test_every_card_row_names_a_property_the_code_reads(cards):
    index = resources.index()
    stale = {}
    for name, fields in cards.items():
        if name not in index["managers"]:
            continue
        canonical, nested = _schema_keys(resources.generated_schema_for(name))
        extra = sorted(set(fields) - canonical - nested)
        if extra:
            stale[name] = extra
    assert stale == {}, f"card rows for properties the code does not read: {stale}"


def test_managers_without_a_card_are_exactly_the_known_ones(cards):
    index = resources.index()
    undocumented = {name for name in index["managers"] if name not in cards}
    assert undocumented == UNDOCUMENTED
    assert set(cards) - set(index["managers"]) - {"PyCoboltManager"} == set(), "cards for managers that do not exist"


def test_piezoconcept_variants_have_separate_cards(cards):
    assert "range_um" in cards["PiezoconceptZManager2"] and "range_um" not in cards["PiezoconceptZManager"]

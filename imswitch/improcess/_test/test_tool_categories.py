"""The analysis-tool categories: one list, used by every built-in, in one order.

The Tools menu shows one submenu per category, so a misspelt category would
quietly open a new submenu with one tool in it. Every built-in processor and
built-in panel must name a category from :data:`TOOL_CATEGORIES`; drop-in
plugins may name their own, which sort after the listed ones.
"""

from __future__ import annotations

from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs, sorted_tool_specs
from imswitch.improcess.processors import (
    _AVAILABLE_PROCESSOR_CLASSES,
    available_processor_specs,
    processor_summary,
)
from imswitch.improcess.processors.categories import (
    OTHER_CATEGORY,
    TOOL_CATEGORIES,
    category_sort_key,
)


def test_every_built_in_tool_names_a_listed_category():
    unknown = {
        tool_id: spec.category
        for tool_id, spec in runtime_analysis_tool_specs(origin="builtin").items()
        if spec.category not in TOOL_CATEGORIES
    }
    assert unknown == {}, f"add these to TOOL_CATEGORIES or fix the spelling: {unknown}"


def test_the_list_has_no_duplicates_and_no_other():
    assert len(set(TOOL_CATEGORIES)) == len(TOOL_CATEGORIES)
    assert OTHER_CATEGORY not in TOOL_CATEGORIES


def test_listed_categories_come_first_in_order_then_unknown_then_other():
    names = ["Zebra", OTHER_CATEGORY, "SMLM", "alpha", "Dimensions and channels", None]
    assert sorted(names, key=category_sort_key) == [
        "Dimensions and channels", "SMLM", "alpha", "Zebra", OTHER_CATEGORY, None,
    ]
    assert category_sort_key(None) == category_sort_key(OTHER_CATEGORY)


def test_misplaced_tools_moved_where_they_belong():
    categories = {pid: category for pid, _name, category in available_processor_specs("builtin")}
    assert categories["drift-correct"] == "Registration"
    assert categories["subtract-background"] == "Filters"
    assert {categories[pid] for pid in ("frc", "psf-resolution", "psf-bead-select")} == {
        "Resolution and PSF"
    }


def test_every_built_in_tool_says_what_it_does():
    silent = [
        tool_id for tool_id, spec in runtime_analysis_tool_specs(origin="builtin").items()
        if not spec.description
    ]
    assert silent == []


def test_summaries_are_one_plain_line():
    for processor_id in _AVAILABLE_PROCESSOR_CLASSES:
        summary = processor_summary(processor_id)
        assert "\n" not in summary and "**" not in summary and "``" not in summary, processor_id
    assert processor_summary("no-such-tool") == ""


def test_tool_specs_sort_by_category_order_then_title():
    ordered = sorted_tool_specs(runtime_analysis_tool_specs(origin="builtin").values())
    keys = [category_sort_key(spec.category) for spec in ordered]
    assert keys == sorted(keys)
    first_category = ordered[0].category
    assert first_category == TOOL_CATEGORIES[0]


def test_workflow_editor_palette_uses_the_same_order():
    from imswitch.improcess.model.workfloweditor.catalog import build_catalog

    categories = build_catalog(user_plugins=False).categories()
    assert list(categories) == sorted(categories, key=category_sort_key)
    assert categories[0] == "Dimensions and channels"

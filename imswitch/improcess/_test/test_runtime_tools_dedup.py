"""Runtime analysis tool choices collapse tools that share one widget.

Multicolor registration + apply are two processors backed by the single
Multicolor panel, so the tool list must show 'Multicolor' once, not twice.
"""
from imswitch.improcess.model.runtime_tools import (
    runtime_analysis_tool_choices,
    runtime_analysis_tool_specs,
)


def test_multicolor_appears_once_in_choices():
    choices = runtime_analysis_tool_choices()
    multicolor = [c for c in choices if c[1] == "Multicolor"]
    assert len(multicolor) == 1, multicolor


def test_choices_have_one_entry_per_widget():
    specs = runtime_analysis_tool_specs()
    choices = runtime_analysis_tool_choices()
    attributes = [specs[tool_id].attribute for tool_id, _title in choices]
    assert len(attributes) == len(set(attributes)), attributes


def test_both_multicolor_specs_retained_for_wiring_and_chain():
    # The collapse is display-only: both processor-backed specs still exist so the
    # result-bridge wiring and the processing chain keep working.
    specs = runtime_analysis_tool_specs()
    assert "multicolor-registration" in specs
    assert "multicolor-apply" in specs
    assert specs["multicolor-registration"].attribute == "multicolorWidget"
    assert specs["multicolor-apply"].attribute == "multicolorWidget"

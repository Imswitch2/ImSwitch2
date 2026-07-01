import pytest

from imswitch.improcess.processors import (
    available_processor_choices,
    available_processor_ids,
    register_processor_by_id,
)
from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
from imswitch.improcess.reconstructors.registry import PluginRegistry


def test_runtime_processor_choices_include_display_names():
    choices = dict(available_processor_choices())

    assert choices["projection"] == "Projection"
    assert choices["colocalization"] == "Colocalization"
    assert choices["multicolor-registration"] == "Multicolor Registration"
    assert choices["multicolor-apply"] == "Multicolor Apply"


def test_runtime_analysis_tool_specs_cover_processors_and_custom_tools():
    specs = runtime_analysis_tool_specs()

    for processor_id in available_processor_ids():
        assert processor_id in specs
        assert specs[processor_id].processor_id == processor_id
    assert specs["roi-manager"].processor_id is None
    assert specs["roi-manager"].widget_kind == "roi-manager"


def test_runtime_analysis_tool_specs_classify_generic_and_custom_widgets():
    specs = runtime_analysis_tool_specs()

    assert specs["drift-correct"].widget_kind == "result-processor"
    assert specs["denoise"].widget_kind == "result-processor"
    assert specs["projection"].widget_kind == "projection"
    assert specs["segmentation"].attribute == "segmentationWidget"


def test_register_processor_by_id_adds_builtin_processor():
    registry = PluginRegistry()

    plugin = register_processor_by_id(registry, "projection")

    assert plugin.id == "projection"
    assert registry.get_processor("projection") is plugin


def test_register_processor_by_id_rejects_unknown_id():
    registry = PluginRegistry()

    with pytest.raises(KeyError):
        register_processor_by_id(registry, "does-not-exist")

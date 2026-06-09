import pytest

from imswitch.improcess.processors import (
    available_processor_choices,
    register_processor_by_id,
)
from imswitch.improcess.reconstructors.registry import PluginRegistry


def test_runtime_processor_choices_include_display_names():
    choices = dict(available_processor_choices())

    assert choices["projection"] == "Projection"
    assert choices["colocalization"] == "Colocalization"
    assert choices["multicolor-registration"] == "Multicolor Registration"
    assert choices["multicolor-apply"] == "Multicolor Apply"


def test_register_processor_by_id_adds_builtin_processor():
    registry = PluginRegistry()

    plugin = register_processor_by_id(registry, "projection")

    assert plugin.id == "projection"
    assert registry.get_processor("projection") is plugin


def test_register_processor_by_id_rejects_unknown_id():
    registry = PluginRegistry()

    with pytest.raises(KeyError):
        register_processor_by_id(registry, "does-not-exist")

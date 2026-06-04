from imswitch.improcess.model.processing_config import (
    is_graph_panel_enabled,
    is_profile_panel_enabled,
    plugin_ids_from_config,
)


def test_graph_panel_hidden_by_default():
    assert not is_graph_panel_enabled({})


def test_graph_panel_can_be_disabled():
    assert not is_graph_panel_enabled({"graphPanel": False})


def test_graph_panel_can_be_enabled_explicitly():
    assert is_graph_panel_enabled({"graphPanel": True})


def test_profile_panel_hidden_by_default():
    assert not is_profile_panel_enabled({})


def test_profile_panel_can_be_disabled():
    assert not is_profile_panel_enabled({"profilePanel": False})


def test_profile_panel_can_be_enabled_explicitly():
    assert is_profile_panel_enabled({"profilePanel": True})


def test_graph_panel_only_config_keeps_standalone_plugin_defaults():
    reconstructors, processors, has_plugin_config = plugin_ids_from_config(
        {"graphPanel": False}
    )

    assert reconstructors == ["view-only"]
    assert processors == ["drift-correct"]
    assert not has_plugin_config


def test_plugin_ids_from_config_reports_explicit_plugin_config():
    reconstructors, processors, has_plugin_config = plugin_ids_from_config(
        {"reconstructors": ["monalisa"], "processors": []}
    )

    assert reconstructors == ["monalisa"]
    assert processors == []
    assert has_plugin_config

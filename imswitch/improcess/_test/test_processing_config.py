import json
from pathlib import Path

from imswitch.improcess.reconstructors import available_reconstructor_ids
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.model.processing_config import (
    is_colocalization_panel_enabled,
    is_frc_panel_enabled,
    is_graph_panel_enabled,
    is_multicolor_panel_enabled,
    is_profile_panel_enabled,
    is_psf_resolution_panel_enabled,
    is_projection_panel_enabled,
    is_roi_manager_panel_enabled,
    is_roi_stats_panel_enabled,
    is_segmentation_panel_enabled,
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


def test_frc_panel_hidden_by_default():
    assert not is_frc_panel_enabled({})


def test_frc_panel_can_be_enabled_explicitly():
    assert is_frc_panel_enabled({"frcPanel": True})


def test_roi_stats_panel_hidden_by_default():
    assert not is_roi_stats_panel_enabled({})


def test_roi_stats_panel_can_be_enabled_explicitly():
    assert is_roi_stats_panel_enabled({"roiStatsPanel": True})


def test_roi_manager_panel_hidden_by_default():
    assert not is_roi_manager_panel_enabled({})


def test_roi_manager_panel_can_be_enabled_explicitly():
    assert is_roi_manager_panel_enabled({"roiManagerPanel": True})


def test_projection_panel_hidden_by_default():
    assert not is_projection_panel_enabled({})


def test_projection_panel_can_be_enabled_explicitly():
    assert is_projection_panel_enabled({"projectionPanel": True})


def test_segmentation_panel_hidden_by_default():
    assert not is_segmentation_panel_enabled({})


def test_segmentation_panel_can_be_enabled_explicitly():
    assert is_segmentation_panel_enabled({"segmentationPanel": True})


def test_psf_resolution_panel_hidden_by_default():
    assert not is_psf_resolution_panel_enabled({})


def test_psf_resolution_panel_can_be_enabled_explicitly():
    assert is_psf_resolution_panel_enabled({"psfResolutionPanel": True})


def test_colocalization_panel_hidden_by_default():
    assert not is_colocalization_panel_enabled({})


def test_colocalization_panel_can_be_enabled_explicitly():
    assert is_colocalization_panel_enabled({"colocalizationPanel": True})


def test_multicolor_panel_hidden_by_default():
    assert not is_multicolor_panel_enabled({})


def test_multicolor_panel_can_be_enabled_explicitly():
    assert is_multicolor_panel_enabled({"multicolorPanel": True})


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


def test_improcess_setup_presets_use_known_processor_ids():
    setup_dir = (
        Path(__file__).resolve().parents[2]
        / "_data"
        / "user_defaults"
        / "imcontrol_setups"
    )
    setup_files = [
        setup_dir / "snouty_processor.json",
        setup_dir / "general_image_processing.json",
        setup_dir / "monalisa_processor.json",
        setup_dir / "widefieldstarss_processor.json",
    ]
    known_processors = set(available_processor_ids())

    for setup_file in setup_files:
        config = json.loads(setup_file.read_text(encoding="utf-8"))
        processors = set(config["processing"].get("processors", []))
        assert not (processors - known_processors), setup_file.name


def test_improcess_setup_presets_use_known_reconstructor_ids():
    setup_dir = (
        Path(__file__).resolve().parents[2]
        / "_data"
        / "user_defaults"
        / "imcontrol_setups"
    )
    setup_files = [
        setup_dir / "snouty_processor.json",
        setup_dir / "general_image_processing.json",
        setup_dir / "monalisa_processor.json",
        setup_dir / "widefieldstarss_processor.json",
    ]
    known_reconstructors = set(available_reconstructor_ids())

    for setup_file in setup_files:
        config = json.loads(setup_file.read_text(encoding="utf-8"))
        reconstructors = set(config["processing"].get("reconstructors", []))
        assert not (reconstructors - known_reconstructors), setup_file.name


def test_snouty_setup_enables_multicolor_workflow():
    setup_file = (
        Path(__file__).resolve().parents[2]
        / "_data"
        / "user_defaults"
        / "imcontrol_setups"
        / "snouty_processor.json"
    )
    processing = json.loads(setup_file.read_text(encoding="utf-8"))["processing"]

    assert processing["multicolorPanel"]
    assert "multicolor-registration" in processing["processors"]
    assert "multicolor-apply" in processing["processors"]

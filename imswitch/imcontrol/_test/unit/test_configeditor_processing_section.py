"""The config editor's ImProcess section offers exactly what ImProcess reads.

A key ImProcess reads but the editor does not offer is invisible: the only way
to set it is to hand-edit the setup JSON. A key the editor offers but ImProcess
ignores is worse. And a default the template states that differs from the one
the reader applies makes the form lie about what an absent key does. All three
are pinned here against ``PROCESSING_KEYS`` and the reader functions
themselves.

Pure JSON and pure Python: nothing here imports Qt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from imswitch.improcess.model import processing_config as pc

_REPO = Path(__file__).resolve().parents[4]
_CONFIG_MODULE = Path(pc.__file__)
_IMPROCESS = _REPO / "imswitch" / "improcess"
_SECTION = (_REPO / "imswitch" / "imcontrol" / "view" / "configeditor"
            / "builtin_templates" / "sections" / "processing.json")

#: Which reader answers for each flag when the key is absent.
_BOOL_READERS = {
    "parameterPanel": pc.is_parameter_panel_enabled,
    "napariLayerControls": pc.are_napari_layer_controls_enabled,
    "reconstructionPanel": pc.is_reconstruction_panel_enabled,
    "actionsPanel": pc.is_actions_panel_enabled,
    "fileWatcherPanel": pc.is_file_watcher_panel_enabled,
    "multiDataPanel": pc.is_multi_data_panel_enabled,
    "currentDataPanel": pc.is_current_data_panel_enabled,
    "resultsPanel": pc.is_results_panel_enabled,
    "logPanel": pc.is_log_panel_enabled,
    "graphPanel": pc.is_graph_panel_enabled,
    "profilePanel": pc.is_profile_panel_enabled,
    "frcPanel": pc.is_frc_panel_enabled,
    "projectionPanel": pc.is_projection_panel_enabled,
    "roiStatsPanel": pc.is_roi_stats_panel_enabled,
    "roiManagerPanel": pc.is_roi_manager_panel_enabled,
    "segmentationPanel": pc.is_segmentation_panel_enabled,
    "psfResolutionPanel": pc.is_psf_resolution_panel_enabled,
    "colocalizationPanel": pc.is_colocalization_panel_enabled,
    "multicolorPanel": pc.is_multicolor_panel_enabled,
    "metadataPanel": pc.is_metadata_panel_enabled,
    "napariStormViewer": pc.is_napari_storm_viewer_enabled,
    "smlmRenderPanel": pc.is_smlm_render_panel_enabled,
    "live_display_in_imcontrol": pc.is_live_display_in_imcontrol_enabled,
}

_GROUPS = ["Startup layout", "Analysis panels", "Localization viewer",
           "Live mode", "Plugins"]


def _section() -> dict:
    return json.loads(_SECTION.read_text(encoding="utf-8"))


def _fields() -> dict[str, dict]:
    return {field["key"]: field for field in _section()["fields"]}


def _improcess_sources_without_the_key_set() -> str:
    """Every non-test improcess source, with the PROCESSING_KEYS literal cut out.

    The set names every key, so searching it for a key would always succeed.
    """
    chunks = []
    for path in _IMPROCESS.rglob("*.py"):
        if "_test" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"PROCESSING_KEYS[^=]*=\s*frozenset\(\{.*?\}\)", "", text, flags=re.S)
        chunks.append(text)
    return "\n".join(chunks)


def test_the_editor_offers_exactly_the_keys_improcess_reads():
    offered = set(_fields())
    missing = sorted(pc.PROCESSING_KEYS - offered)
    unread = sorted(offered - pc.PROCESSING_KEYS)
    assert not missing, (
        f"read by ImProcess but absent from {_SECTION.relative_to(_REPO)}: {missing}")
    assert not unread, f"offered by the editor but never read by ImProcess: {unread}"


def test_the_template_lists_each_key_once():
    keys = [field["key"] for field in _section()["fields"]]
    assert len(keys) == len(set(keys))


def test_every_literal_the_config_module_reads_is_in_the_key_set():
    """A flag added to processing_config.py without joining PROCESSING_KEYS."""
    source = _CONFIG_MODULE.read_text(encoding="utf-8")
    read = set(re.findall(r'processing_config\.get\(\s*"([A-Za-z0-9_]+)"', source))
    read |= set(re.findall(r'\bkey\s*=\s*"([A-Za-z0-9_]+)"', source))
    assert read, "the scan found nothing; the pattern is stale"
    assert not (read - pc.PROCESSING_KEYS), sorted(read - pc.PROCESSING_KEYS)


def test_every_key_in_the_set_is_still_read_somewhere():
    """A key left in PROCESSING_KEYS after its reader was deleted."""
    sources = _improcess_sources_without_the_key_set()
    stale = sorted(key for key in pc.PROCESSING_KEYS
                   if f'"{key}"' not in sources and f"'{key}'" not in sources)
    assert not stale, f"in PROCESSING_KEYS but no longer read under imswitch/improcess: {stale}"


def test_the_set_has_a_reader_default_for_every_flag():
    flags = {key for key, field in _fields().items() if field["type"] == "bool"}
    assert flags == set(_BOOL_READERS), sorted(flags ^ set(_BOOL_READERS))


@pytest.mark.parametrize("key", sorted(_BOOL_READERS))
def test_a_flag_defaults_to_what_its_reader_answers_for_an_absent_key(key):
    field = _fields()[key]
    assert field["type"] == "bool", f"{key} should be a checkbox"
    assert field["default"] is _BOOL_READERS[key]({}), (
        f"{key}: the form says {field['default']!r}, the reader says "
        f"{_BOOL_READERS[key]({})!r}")


def test_live_viewer_update_interval_default_is_the_readers():
    field = _fields()["liveViewerUpdateIntervalS"]
    assert field["type"] == "float"
    assert field["default"] == pc.live_viewer_update_interval_s({})


def test_live_stall_timeout_is_left_absent_by_default():
    """Absent is the automatic mode; a number would pin it everywhere."""
    field = _fields()["liveStallTimeoutS"]
    assert field["type"] == "float"
    assert field["default"] in (None, "null")
    assert pc.live_stall_timeout_s({})[1] is False


def test_the_zeros_the_tips_promise_are_what_the_readers_do():
    """The tips say 0 switches the watchdog off and refreshes every frame."""
    stall, update = _fields()["liveStallTimeoutS"]["tip"], _fields()["liveViewerUpdateIntervalS"]["tip"]
    assert "0 = watchdog off" in stall
    assert pc.live_stall_timeout_s({"liveStallTimeoutS": 0}) == (None, True)
    assert "0 = refresh on every frame" in update
    assert pc.live_viewer_update_interval_s({"liveViewerUpdateIntervalS": 0}) == 0.0


def test_live_display_in_imcontrol_reads_the_snake_case_key():
    assert pc.is_live_display_in_imcontrol_enabled({"live_display_in_imcontrol": True})
    assert not pc.is_live_display_in_imcontrol_enabled({"liveDisplayInImcontrol": True})
    assert "live_display_in_imcontrol" in _fields()


def test_plugin_lists_default_to_what_plugin_ids_from_config_answers():
    fields = _fields()
    reconstructors, processors, _ = pc.plugin_ids_from_config({})
    assert fields["reconstructors"]["default"] == reconstructors
    assert fields["processors"]["default"] == processors
    assert fields["reconstructors"]["req"] is True
    assert fields["processors"]["req"] is False


def test_napari_endpoints_is_an_empty_list_by_default():
    field = _fields()["napariEndpoints"]
    assert field["type"] == "json"
    assert field["default"] == []


def test_the_section_asks_to_be_written_as_written():
    assert _section()["write_policy"] == "as_written"


def test_only_reconstructors_is_required():
    required = [key for key, field in _fields().items() if field["req"]]
    assert required == ["reconstructors"]


def test_groups_are_the_five_in_order():
    seen: list[str] = []
    for field in _section()["fields"]:
        if field["grp"] not in seen:
            seen.append(field["grp"])
    assert seen == _GROUPS
    # contiguous: a group never reappears after another one started
    order = [field["grp"] for field in _section()["fields"]]
    assert order == sorted(order, key=_GROUPS.index)


def test_per_computer_settings_stay_out_of_the_template():
    """They live in improcess_options.json, not in a setup file."""
    for key in ("fijiDir", "fijiPython", "imageDisplay", "folders"):
        assert key not in _fields()


def test_the_napari_storm_flag_says_it_needs_the_optional_extra():
    """Turning it on with no package installed does nothing; say so up front."""
    field = _fields()["napariStormViewer"]
    assert field["type"] == "bool"
    assert field["default"] is False
    assert "storm" in field["tip"], "the tooltip must name the extra to install"


def test_panels_that_register_a_processor_say_so():
    """projection and frc add their processor at startup; no other panel does."""
    fields = _fields()
    assert "'projection' processor" in fields["projectionPanel"]["tip"]
    assert "'frc' processor" in fields["frcPanel"]["tip"]

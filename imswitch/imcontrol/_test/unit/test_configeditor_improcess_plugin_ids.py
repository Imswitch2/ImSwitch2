"""The editor tells built-in ImProcess plugin ids from drop-in ids from unknown ids.

Built-in ids come from ImProcess's built-in tables (never from the editor's
option lists, which also carry any drop-in already loaded in this process).
The drop-in folder is discovered once per editor session, and only when a
setup lists an id that is not built-in: discovery imports the plugin files.
"""

import copy
import sys
import textwrap

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QPushButton

from imswitch.imcontrol.view.configeditor import editor

PLUGIN_SOURCE = textwrap.dedent('''
    from imswitch.improcess.processors.base import Processor

    class TinyProcessor(Processor):
        name = "Tiny (test)"
        id = "user.tiny-test"
        category = "User"
        kinds = ("image",)
''')


@pytest.fixture
def dropin_folder(tmp_path, monkeypatch):
    """A plugins folder with one drop-in, installed as the default folder, and
    an empty session cache."""
    folder = tmp_path / "improcess_plugins"
    folder.mkdir()
    (folder / "tiny.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    import imswitch.improcess.plugins.user_plugins as user_plugins

    monkeypatch.setattr(user_plugins, "user_plugins_directory", lambda create=True: str(folder))
    monkeypatch.setattr(editor, "_DROPIN_CACHE", {})
    yield folder
    sys.modules.pop("improcess_user_plugin_tiny", None)


@pytest.fixture
def discoveries(monkeypatch):
    """Count how often the plugin files are really scanned."""
    calls = []
    real = editor._discover_dropins
    monkeypatch.setattr(editor, "_discover_dropins", lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


def _issues(processing):
    return editor._collect_xref_issues({"processing": processing})


def _codes_text(issues):
    return {sev: [msg for s, msg in issues if s == sev] for sev in ("error", "warning")}


def test_a_builtin_only_block_is_clean_and_imports_no_plugin_file(dropin_folder, discoveries):
    issues = _issues({"reconstructors": ["view-only", "monalisa"], "processors": ["resize", "drift-correct"]})
    assert [i for i in issues if "processing" in i[1]] == []
    assert discoveries == [], "built-in ids need no discovery"


def test_a_dropin_id_in_the_setup_is_a_portability_warning(dropin_folder):
    found = _codes_text(_issues({"reconstructors": ["view-only"], "processors": ["user.tiny-test", "resize"]}))
    assert found["error"] == []
    assert len(found["warning"]) == 1
    assert "user.tiny-test" in found["warning"][0]
    assert "drop-in plugin on this computer" in found["warning"][0]


def test_an_unknown_id_is_an_error_that_says_it_is_skipped(dropin_folder):
    found = _codes_text(_issues({"reconstructors": ["view-only", "nope"], "processors": ["nope2"]}))
    assert len(found["error"]) == 2
    assert all("ImProcess skips unknown ids at startup" in m for m in found["error"])
    assert found["warning"] == []


def test_the_drop_in_folder_is_discovered_once_per_session(dropin_folder, discoveries):
    for _ in range(3):
        _issues({"processors": ["user.tiny-test"]})
        _issues({"processors": ["nope"]})
    assert len(discoveries) == 1


def test_without_discovery_a_dropin_is_just_unknown(dropin_folder, monkeypatch):
    monkeypatch.setitem(sys.modules, "imswitch.improcess.plugins.user_plugins", None)
    found = _codes_text(_issues({"processors": ["user.tiny-test"]}))
    assert len(found["error"]) == 1 and found["warning"] == []


def test_the_builtin_ids_exclude_a_dropin_already_loaded_in_this_process(monkeypatch):
    from imswitch.improcess.processors import _USER_PROCESSOR_CLASSES, _all_processor_classes

    monkeypatch.setitem(_USER_PROCESSOR_CLASSES, "user.loaded", _all_processor_classes()["resize"])
    reconstructors, processors = editor._improcess_builtin_ids()
    assert "user.loaded" not in processors
    assert "view-only" in reconstructors and "resize" in processors


def test_the_builtin_ids_fall_back_to_the_offline_lists(monkeypatch):
    monkeypatch.setitem(sys.modules, "imswitch.improcess.reconstructors", None)
    reconstructors, processors = editor._improcess_builtin_ids()
    assert reconstructors == tuple(editor.IMPROCESS_RECONSTRUCTOR_FALLBACK)
    assert processors == tuple(editor.IMPROCESS_PROCESSOR_FALLBACK)


def test_the_validation_panel_shows_them(qapp, dropin_folder):
    panel = editor.ValidationPanel()
    panel.validate({"processing": {"processors": ["user.tiny-test", "nope"], "reconstructors": ["view-only"]}})
    text = panel._text.text()
    assert "drop-in plugin on this computer" in text
    assert "ImProcess skips unknown ids at startup" in text


# ── the Rescan button on the drop-in page ───────────────────────────────────
def _dialog(current, **kw):
    return editor.ImProcessSectionDialog(editor.SECTION_SCHEMAS["processing"], current, {}, **kw)


def _rows(dlg):
    tree = dlg._dropin_tree
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def test_the_dialog_shares_the_session_scan_and_rescan_refreshes_it(qapp, dropin_folder, discoveries):
    _issues({"processors": ["user.tiny-test"]})
    assert len(discoveries) == 1
    dlg = _dialog({"reconstructors": ["view-only"]})
    assert len(discoveries) == 1, "the dialog reuses the scan the validation made"
    assert _rows(dlg) == ["user.tiny-test"]

    (dropin_folder / "tiny.py").unlink()
    sys.modules.pop("improcess_user_plugin_tiny", None)
    dlg._page_list.setCurrentRow(dlg._page_titles.index("Drop-in plugins"))
    next(b for b in dlg.findChildren(QPushButton) if b.text() == editor.DROPIN_RESCAN_BUTTON).click()
    assert len(discoveries) == 2
    assert _rows(dlg) == []
    assert dlg._page_list.currentRow() == dlg._page_titles.index("Drop-in plugins")
    assert dlg._stack.currentIndex() == dlg._page_titles.index("Drop-in plugins")
    # and the validation sees the refreshed scan
    found = _codes_text(_issues({"processors": ["user.tiny-test"]}))
    assert len(found["error"]) == 1 and found["warning"] == []


def test_applying_after_a_rescan_still_writes_the_block_as_it_was(qapp, dropin_folder):
    block = {"reconstructors": ["view-only"], "processors": ["user.tiny-test"]}
    dlg = _dialog(copy.deepcopy(block))
    next(b for b in dlg.findChildren(QPushButton) if b.text() == editor.DROPIN_RESCAN_BUTTON).click()
    dlg._try_apply()
    assert dlg.result_data == block


# ── the tips ────────────────────────────────────────────────────────────────
def _tip(key):
    return editor._schema_fields(editor.SECTION_SCHEMAS["processing"])[key]["tip"]


@pytest.mark.parametrize("key", ["reconstructors", "processors"])
def test_the_plugin_list_tips_explain_builtins_dropins_and_skipping(key):
    tip = _tip(key)
    assert "picks which built-ins are registered" in tip
    assert "Drop-in plugins page" in tip and "should not be listed" in tip
    assert "skipped at startup with a warning" in tip


def test_the_processors_tip_names_the_self_registering_panels_and_the_runtime_menu():
    tip = _tip("processors")
    for panel in ("projection", "frc"):
        assert panel in tip
        assert f"registers the '{panel}' processor" in _tip(f"{panel}Panel")
    assert "Load tool" in tip


def test_the_runtime_loading_paths_named_in_the_tips_exist_in_improcess():
    from pathlib import Path

    source = (Path(editor.__file__).resolve().parents[3] / "improcess" / "view"
              / "ImProcessMainView.py").read_text(encoding="utf-8")
    assert "'Load tool: '" in source
    assert "addMenu('Load reconstructor')" in source
    assert "Tools > Load reconstructor" in _tip("reconstructors")

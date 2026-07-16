"""ImProcess configurable shortcuts: catalog, Fiji defaults, persistence."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.imcommon.controller.ShortcutManager import ShortcutManager
from imswitch.improcess.controller import shortcuts as shortcuts_module
from imswitch.improcess.controller.shortcuts import (
    improcess_shortcut_defaults,
    load_shortcut_overrides,
    register_improcess_shortcuts,
    save_shortcut_overrides,
)
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)

    def __call__(self, *args):
        # QAction.triggered.connect(...) needs a callable.
        self.emit(*args)


def _stub_view():
    # A real QWidget: the manager parents its QActions to the action owner.
    view = QtWidgets.QWidget()
    for _aid, _name, _key, signal_name in shortcuts_module._SIGNAL_SPECS:
        setattr(view, signal_name, _Signal())
    view.sigLoadProcessorRequested = _Signal()
    view.raised = []
    view.raiseDockByTitle = view.raised.append
    return view


# -- Fiji-parity defaults -----------------------------------------------------

def test_fiji_parity_defaults():
    defaults = improcess_shortcut_defaults()
    assert defaults["file.quick-load"] == "Ctrl+O"            # File > Open
    assert defaults["file.save-reconstruction"] == "Ctrl+S"   # File > Save
    assert defaults["image.brightness-contrast"] == "Ctrl+Shift+C"
    assert defaults["image.channels"] == "Ctrl+Shift+Z"       # Channels Tool
    assert defaults["image.duplicate"] == "Ctrl+Shift+D"
    assert defaults["image.crop-substack"] == "Ctrl+Shift+X"
    assert defaults["panel.graph"] == "Ctrl+H"                # Histogram
    assert defaults["panel.profile"] == "Ctrl+K"              # Plot Profile
    assert defaults["panel.roi-manager"] == "Ctrl+T"          # ROI Manager
    assert defaults["panel.roi-stats"] == "Ctrl+M"            # Measure
    # New/exotic operations ship unbound but rebindable.
    assert defaults["image.stack-combine"] is None
    assert defaults["image.image-calculator"] is None


def test_no_two_default_bindings_collide():
    defaults = improcess_shortcut_defaults()
    bound = [key for key in defaults.values() if key]
    assert len(bound) == len(set(bound)), sorted(bound)


# -- registration on the shared manager ------------------------------------------

def test_catalog_registers_every_action_and_fires_signals(qapp):
    view = _stub_view()
    manager = ShortcutManager()
    register_improcess_shortcuts(manager, view)

    actions = manager.getAllActions()
    assert set(actions) == set(improcess_shortcut_defaults())

    # Signal-backed action fires its view signal.
    actions["image.duplicate"].callback()
    assert view.sigImageDuplicateRequested.emitted == [()]
    # Panel action loads the runtime tool by id.
    actions["panel.roi-manager"].callback()
    assert view.sigLoadProcessorRequested.emitted == [("roi-manager",)]
    # Results-table action raises the dock.
    actions["panel.results-table"].callback()
    assert view.raised == ["Results"]


def test_overrides_change_effective_bindings(qapp):
    view = _stub_view()
    manager = ShortcutManager()
    register_improcess_shortcuts(manager, view)
    manager.loadConfigOverrides(
        {"image.duplicate": "F9", "image.brightness-contrast": None}
    )
    manager.computeEffectiveBindings()

    effective = manager.getEffectiveBindings()
    assert effective.get("image.duplicate") == "F9"
    assert effective.get("image.brightness-contrast") is None  # disabled
    assert effective.get("image.crop-substack") == "Ctrl+Shift+X"  # untouched


# -- per-user persistence -----------------------------------------------------------

def test_overrides_round_trip_through_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shortcuts_module.dirtools.UserFileDirs, "Root", str(tmp_path)
    )
    assert load_shortcut_overrides() == {}  # no file yet

    save_shortcut_overrides({"image.duplicate": "F9", "panel.graph": None})
    assert load_shortcut_overrides() == {
        "image.duplicate": "F9",
        "panel.graph": None,
    }


def test_corrupt_overrides_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shortcuts_module.dirtools.UserFileDirs, "Root", str(tmp_path)
    )
    (tmp_path / shortcuts_module.SHORTCUTS_FILENAME).write_text("{broken", encoding="utf-8")
    assert load_shortcut_overrides() == {}


# -- display-only key hints on menu actions ------------------------------------------

def test_action_shortcut_displays_are_display_only(qapp):
    view = QtWidgets.QMainWindow()
    action = QtWidgets.QAction("Duplicate", view)
    action.setToolTip("Duplicate the active result")
    view._shortcutActions = {"image.duplicate": action}

    ImProcessMainView.updateActionShortcutDisplays(
        view, {"image.duplicate": "Ctrl+Shift+D"}
    )
    assert action.text() == "Duplicate\tCtrl+Shift+D"
    assert "Ctrl+Shift+D" in action.toolTip()
    # Display only: no real QAction binding (the manager owns the key).
    assert action.shortcut().isEmpty()

    # Rebinding updates the hint from the stored base text (no accumulation).
    ImProcessMainView.updateActionShortcutDisplays(view, {"image.duplicate": "F9"})
    assert action.text() == "Duplicate\tF9"
    ImProcessMainView.updateActionShortcutDisplays(view, {})
    assert action.text() == "Duplicate"


# -- shared editor persists via callback -----------------------------------------------

def test_editor_persists_through_callback(qapp):
    from imswitch.imcommon.view.ShortcutEditorDialog import ShortcutEditorDialog

    view = QtWidgets.QMainWindow()
    view.shortcutsMenu = QtWidgets.QMenu("&Shortcuts", view)
    stub = _stub_view()
    manager = ShortcutManager()
    register_improcess_shortcuts(manager, stub)
    manager.computeEffectiveBindings()

    saved = []
    dialog = ShortcutEditorDialog(view, manager, persistCallback=saved.append)
    dialog._pendingChanges["image.duplicate"] = "F9"
    dialog._onOk()

    assert len(saved) == 1
    assert saved[0].get("image.duplicate") == "F9"
    # Only diffs from defaults are persisted.
    assert "image.crop-substack" not in saved[0]


# -- module isolation: only the visible tab's set is live ----------------------------

def _built_qt_actions(manager):
    return [obj for objs in manager._qtObjects.values() for obj in objs]


def test_bindings_toggle_with_module_visibility(qapp):
    view = _stub_view()
    window = QtWidgets.QMainWindow()
    window.shortcutsMenu = QtWidgets.QMenu("&Shortcuts", window)
    manager = ShortcutManager()
    register_improcess_shortcuts(manager, view)
    manager.computeEffectiveBindings()
    manager.build(window.shortcutsMenu, window)

    built = _built_qt_actions(manager)
    assert built and all(action.isEnabled() for action in built)

    manager.setBindingsEnabled(False)  # tab hidden
    assert all(not action.isEnabled() for action in built)
    manager.setBindingsEnabled(True)  # tab shown again
    assert all(action.isEnabled() for action in built)


def test_disabled_state_survives_a_rebuild(qapp):
    """The editor's Apply rebuilds all bindings; a hidden module must stay off."""
    view = _stub_view()
    window = QtWidgets.QMainWindow()
    window.shortcutsMenu = QtWidgets.QMenu("&Shortcuts", window)
    manager = ShortcutManager()
    register_improcess_shortcuts(manager, view)
    manager.computeEffectiveBindings()
    manager.setBindingsEnabled(False)

    manager.build(window.shortcutsMenu, window)

    built = _built_qt_actions(manager)
    assert built and all(not action.isEnabled() for action in built)


def test_view_show_hide_emits_visibility_signal(qapp):
    view = ImProcessMainView.__new__(ImProcessMainView)
    QtWidgets.QMainWindow.__init__(view)
    states = []
    view.sigModuleVisibilityChanged.connect(states.append)

    view.show()
    view.hide()

    assert states == [True, False]


# -- preferences menu placement -----------------------------------------------------------

def test_module_settings_live_in_preferences_menu_not_tools(qapp):
    from imswitch.imcommon.view.MultiModuleWindow import MultiModuleWindow

    window = QtWidgets.QMainWindow()
    window.sigPickModules = _Signal()
    window.sigOpenUserDir = _Signal()
    window.sigShowDocs = _Signal()
    window.sigCheckUpdates = _Signal()
    window.sigShowAbout = _Signal()
    menuBar = QtWidgets.QMenuBar(window)
    toolsMenu = menuBar.addMenu("&Tools")  # module's own tools menu

    MultiModuleWindow.addItemsToMenuBar(window, menuBar)

    menus = {
        menu.title(): menu
        for menu in menuBar.findChildren(QtWidgets.QMenu)
    }
    assert "&Preferences" in menus
    preference_texts = [action.text() for action in menus["&Preferences"].actions()]
    assert "Set active modules…" in preference_texts
    assert "Open user files folder" in preference_texts
    # The module's Tools menu is left alone.
    assert toolsMenu.actions() == []

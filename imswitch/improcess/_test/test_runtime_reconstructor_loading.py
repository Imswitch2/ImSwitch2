"""Tools -> Load reconstructor: bring a known reconstructor in for the session.

The Parameters-dock picker offers only registered reconstructors, and the
setup file decides which built-ins those are. The submenu lists every
reconstructor ImProcess knows about but has not loaded -- built-ins the setup
file did not name, drop-ins discovered but not registered -- and picking one
registers it, offers it in the picker and makes it active.
"""

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qtpy import QtWidgets

import imswitch.improcess.plugins as plugins
from imswitch.improcess.controller.ImProcessMainController import (
    ImProcessMainController as Controller,
)
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController as Manager,
)
from imswitch.improcess.reconstructors import (
    available_reconstructor_ids,
    available_reconstructor_specs,
    builtin_reconstructor_ids,
    register_default_reconstructors,
)
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.registry import PluginRegistry
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_user_plugins():
    plugins.clear_user_plugins()
    yield
    plugins.clear_user_plugins()


# --- enumeration -----------------------------------------------------------


def test_reconstructor_specs_cover_every_known_id_with_descriptions():
    specs = available_reconstructor_specs()

    assert [plugin_id for plugin_id, _name, _description in specs] == available_reconstructor_ids()
    by_id = {plugin_id: (name, description) for plugin_id, name, description in specs}
    assert by_id["view-only"] == ("View only", "Display raw frames without any reconstruction")
    assert by_id["monalisa"][0] == "MoNaLISA"
    assert all(isinstance(description, str) for _name, description in by_id.values())


def test_reconstructor_specs_include_discovered_dropins(tmp_path):
    (tmp_path / "mean.py").write_text(
        textwrap.dedent(
            '''
            from imswitch.improcess.reconstructors.base import Reconstructor


            class MeanReconstructor(Reconstructor):
                name = "Mean of frames"
                id = "user.mean"
                description = "Average every frame"

                @classmethod
                def default_params(cls):
                    return {}

                def make_param_widget(self, parent):
                    return None

                def make_metadata_dialog(self, parent):
                    return None

                def process(self, data_obj, params, context=None):
                    return None
            '''
        ),
        encoding="utf-8",
    )
    plugins.load_user_plugins(str(tmp_path))

    by_id = {plugin_id: (name, description) for plugin_id, name, description in available_reconstructor_specs()}
    assert by_id["user.mean"] == ("Mean of frames", "Average every frame")
    assert "user.mean" not in builtin_reconstructor_ids()


# --- view ------------------------------------------------------------------


def _view():
    """The view's own methods over a bare window, as the other menu tests do."""
    view = QtWidgets.QMainWindow()
    view._analysisMenu = QtWidgets.QMenu()
    view.sigLoadReconstructorRequested = _Signal()
    view.setAvailableReconstructors = (
        lambda choices, **kwargs: ImProcessMainView.setAvailableReconstructors(
            view, choices, **kwargs
        )
    )
    ImProcessMainView._buildLoadReconstructorMenu(view)
    return view


def test_load_reconstructor_menu_starts_with_a_disabled_placeholder(qapp):
    view = _view()

    submenu_actions = [a for a in view._analysisMenu.actions() if a.menu() is not None]
    assert [a.menu().title() for a in submenu_actions] == ["Load reconstructor"]
    entries = view._loadReconstructorMenu.actions()
    assert [a.text() for a in entries] == ["All reconstructors loaded"]
    assert not entries[0].isEnabled()
    assert view._loadReconstructorActions == {}


def test_load_reconstructor_menu_lists_choices_and_emits_the_id(qapp):
    view = _view()

    ImProcessMainView.setAvailableReconstructors(
        view,
        [
            ("monalisa", "MoNaLISA", "Point-scanning SIM"),
            ("user.mean", "Mean of frames", ""),
        ],
    )

    entries = view._loadReconstructorMenu.actions()
    assert [a.text() for a in entries] == ["MoNaLISA", "Mean of frames"]
    assert all(a.isEnabled() for a in entries)
    assert entries[0].toolTip() == "Point-scanning SIM"
    assert entries[1].toolTip() == "Load the Mean of frames reconstructor"
    assert set(view._loadReconstructorActions) == {"monalisa", "user.mean"}

    view._loadReconstructorActions["user.mean"].trigger()
    assert view.sigLoadReconstructorRequested.emitted == [("user.mean",)]

    # Once everything is loaded the placeholder is back.
    ImProcessMainView.setAvailableReconstructors(view, [])
    entries = view._loadReconstructorMenu.actions()
    assert [a.text() for a in entries] == ["All reconstructors loaded"]
    assert not entries[0].isEnabled()


# --- controller ------------------------------------------------------------


def _controller(registry, monkeypatch, *, activate=True):
    """The controller's own methods over a minimal surface."""
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.registry.get_registry", lambda: registry
    )
    offered, status, loaded = [], [], []
    stub = SimpleNamespace()
    stub._ImProcessMainController__logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    stub._ImProcessMainController__mainView = SimpleNamespace(
        setAvailableReconstructors=lambda choices: offered.append(list(choices)),
        showStatusMessage=lambda message, **k: status.append(message),
    )
    stub.mainViewController = SimpleNamespace(
        reconstructorManager=SimpleNamespace(
            reconstructorLoaded=lambda pid: loaded.append(pid) or activate
        )
    )
    stub._refresh_reconstructor_choices = (
        lambda: Controller._refresh_reconstructor_choices(stub)
    )
    stub._show_status_message = lambda m: Controller._show_status_message(stub, m)
    return stub, offered, status, loaded


def test_refresh_offers_what_is_known_but_not_registered(monkeypatch):
    registry = PluginRegistry()
    register_default_reconstructors(registry, ["view-only"])
    stub, offered, _status, _loaded = _controller(registry, monkeypatch)

    Controller._refresh_reconstructor_choices(stub)

    ids = [plugin_id for plugin_id, _name, _description in offered[-1]]
    assert "view-only" not in ids
    assert "monalisa" in ids and "smlm-localizer" in ids
    assert ids == sorted(set(available_reconstructor_ids()) - {"view-only"})


def test_load_registers_offers_and_activates(monkeypatch):
    registry = PluginRegistry()
    register_default_reconstructors(registry, ["view-only"])
    stub, offered, status, loaded = _controller(registry, monkeypatch)

    Controller._load_runtime_reconstructor(stub, "snouty-projections")

    plugin = registry.get_reconstructor("snouty-projections")
    assert plugin.id == "snouty-projections"
    assert loaded == ["snouty-projections"]          # the manager was asked to activate it
    assert status and "now the active reconstructor" in status[-1]
    # The menu no longer offers what is loaded.
    ids = [plugin_id for plugin_id, _name, _description in offered[-1]]
    assert "snouty-projections" not in ids and "view-only" not in ids


def test_load_says_so_when_the_current_data_is_not_accepted(monkeypatch):
    registry = PluginRegistry()
    stub, _offered, status, loaded = _controller(registry, monkeypatch, activate=False)

    Controller._load_runtime_reconstructor(stub, "view-only")

    assert registry.get_reconstructor("view-only", raise_on_missing=False) is not None
    assert loaded == ["view-only"]
    assert status and "select it in the Parameters dock" in status[-1]


def test_loading_an_already_loaded_reconstructor_keeps_the_instance(monkeypatch):
    registry = PluginRegistry()
    register_default_reconstructors(registry, ["view-only"])
    before = registry.get_reconstructor("view-only")
    stub, _offered, _status, loaded = _controller(registry, monkeypatch)

    Controller._load_runtime_reconstructor(stub, "view-only")

    assert registry.get_reconstructor("view-only") is before
    assert loaded == ["view-only"]


def test_loading_an_unknown_id_reports_and_registers_nothing(monkeypatch):
    registry = PluginRegistry()
    stub, offered, status, loaded = _controller(registry, monkeypatch)

    Controller._load_runtime_reconstructor(stub, "nope")

    assert registry.reconstructors() == []
    assert loaded == []
    assert status and "Could not load reconstructor 'nope'" in status[-1]
    assert offered                                   # the menu was refreshed anyway


# --- manager ---------------------------------------------------------------


class _Stub(Reconstructor):
    name = "Stub"
    id = "stub"

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        return None


def test_manager_offers_then_activates_and_reports(monkeypatch):
    plugin = _Stub()
    calls = []
    manager = SimpleNamespace(
        _main=SimpleNamespace(_activeReconstructor=None),
        _publishReconstructorChoices=lambda: calls.append("publish"),
    )

    def _activate(plugin_id):
        calls.append(("activate", plugin_id))
        manager._main._activeReconstructor = plugin

    manager._on_user_changed_reconstructor = _activate

    assert Manager.reconstructorLoaded(manager, "stub") is True
    assert calls == ["publish", ("activate", "stub")]

    # When the swap did not happen (source not accepted), the caller is told.
    manager._on_user_changed_reconstructor = lambda plugin_id: calls.append(("refused", plugin_id))
    manager._main._activeReconstructor = None
    assert Manager.reconstructorLoaded(manager, "stub") is False

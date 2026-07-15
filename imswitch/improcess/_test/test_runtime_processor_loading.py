import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qtpy import QtWidgets

from imswitch.improcess.processors import (
    available_processor_choices,
    available_processor_ids,
    available_processor_specs,
    register_processor_by_id,
)
from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
from imswitch.improcess.model.runtime_tools import (
    runtime_analysis_panel_shortcuts,
    runtime_analysis_tool_specs,
)
from imswitch.improcess.reconstructors.registry import PluginRegistry, get_registry
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _Logger:
    def __init__(self):
        self.info_messages = []
        self.exceptions = []

    def info(self, message):
        self.info_messages.append(message)

    def exception(self, message):
        self.exceptions.append(message)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def test_runtime_processor_choices_include_display_names():
    choices = dict(available_processor_choices())

    assert choices["projection"] == "Projection"
    assert choices["colocalization"] == "Colocalization"
    assert choices["multicolor-registration"] == "Multicolor Registration"
    assert choices["multicolor-apply"] == "Multicolor Apply"


def test_runtime_processor_specs_include_categories():
    specs = {
        processor_id: (name, category)
        for processor_id, name, category in available_processor_specs()
    }

    assert specs["projection"] == ("Projection", "Dimensions and channels")
    assert specs["drift-correct"] == ("Drift Correction", "Restoration")
    assert specs["frc"] == ("FRC Resolution", "Measurement")
    assert specs["smlm-render"] == ("SMLM render", "Localization")


def test_runtime_analysis_tool_specs_cover_processors_and_custom_tools():
    specs = runtime_analysis_tool_specs()

    for processor_id in available_processor_ids():
        assert processor_id in specs
        assert specs[processor_id].processor_id == processor_id
        assert specs[processor_id].category
    assert specs["roi-manager"].processor_id is None
    assert specs["roi-manager"].widget_kind == "roi-manager"
    assert specs["roi-manager"].category == "ROI"


def test_runtime_analysis_tool_specs_classify_generic_and_custom_widgets():
    specs = runtime_analysis_tool_specs()

    assert specs["drift-correct"].widget_kind == "result-processor"
    assert specs["denoise"].widget_kind == "result-processor"
    assert specs["projection"].widget_kind == "result-processor"
    # FRC's custom panel was retired in result-unification Phase 4; the
    # processor param widget has full parity and the curve renders in the
    # Graph dock from FRCResult plot payloads.
    assert specs["frc"].widget_kind == "result-processor"
    assert specs["psf-resolution"].widget_kind == "psf-resolution"
    assert specs["colocalization"].widget_kind == "colocalization"
    assert specs["segmentation"].attribute == "segmentationWidget"
    assert specs["graph"].widget_kind == "graph"
    assert specs["profile"].widget_kind == "profile"
    assert specs["roi-stats"].widget_kind == "roi-stats"


def test_runtime_analysis_panel_shortcuts_cover_fiji_like_panels():
    shortcuts = runtime_analysis_panel_shortcuts()

    assert [shortcut.id for shortcut in shortcuts] == [
        "graph",
        "profile",
        "roi-manager",
        "roi-stats",
        "projection",
        "segmentation",
    ]
    assert len({shortcut.id for shortcut in shortcuts}) == len(shortcuts)
    specs = runtime_analysis_tool_specs()
    assert all(shortcut.id in specs for shortcut in shortcuts)


def test_analysis_panel_shortcut_action_emits_runtime_tool_id(qapp):
    view = QtWidgets.QMainWindow()
    view._processorToolbar = QtWidgets.QToolBar()
    view._analysisMenu = QtWidgets.QMenu()
    view._analysisToolActions = {}
    view.sigLoadProcessorRequested = _Signal()

    icon = qapp.style().standardIcon(QtWidgets.QStyle.SP_FileDialogListView)
    action = ImProcessMainView._addAnalysisToolAction(
        view,
        "roi-manager",
        "ROI manager",
        "Open the ROI manager panel",
        icon,
    )
    action.trigger()

    assert view.sigLoadProcessorRequested.emitted == [("roi-manager",)]
    assert view._analysisToolActions["roi-manager"] is action
    assert action in view._processorToolbar.actions()
    assert action in view._analysisMenu.actions()


def test_build_analysis_panel_shortcuts_adds_all_actions(qapp):
    view = QtWidgets.QMainWindow()
    view._processorToolbar = QtWidgets.QToolBar()
    view._analysisMenu = QtWidgets.QMenu()
    view._analysisToolActions = {}
    view.sigLoadProcessorRequested = _Signal()
    view._addAnalysisToolAction = (
        lambda *args: ImProcessMainView._addAnalysisToolAction(view, *args)
    )
    view._addAnalysisDockAction = (
        lambda *args: ImProcessMainView._addAnalysisDockAction(view, *args)
    )

    ImProcessMainView._buildAnalysisToolShortcuts(view)

    expected_ids = [shortcut.id for shortcut in runtime_analysis_panel_shortcuts()]
    expected_ids.append("results-table")
    assert list(view._analysisToolActions) == expected_ids
    assert len(view._processorToolbar.actions()) == len(expected_ids)
    assert len(view._analysisMenu.actions()) == len(expected_ids)
    assert all(
        view._analysisToolActions[tool_id].toolTip()
        for tool_id in expected_ids
    )


def test_plugins_menu_actions_and_reload_signal(qapp):
    view = QtWidgets.QMainWindow()
    view._pluginsMenu = QtWidgets.QMenu()
    view._pluginsToolbar = QtWidgets.QToolBar()
    view.sigReloadPluginsRequested = _Signal()

    ImProcessMainView._buildPluginsMenu(view)

    assert set(view._pluginMenuActions) == {"browse-online", "open-folder", "reload"}
    assert view._pluginMenuActions["open-folder"].text() == "Open plugins folder..."
    assert view._pluginMenuActions["browse-online"].text() == "Browse online plugins..."
    # Store and reload live on the Plugins toolbar too (folder is menu-only).
    toolbar_actions = view._pluginsToolbar.actions()
    assert view._pluginMenuActions["browse-online"] in toolbar_actions
    assert view._pluginMenuActions["reload"] in toolbar_actions
    assert view._pluginMenuActions["open-folder"] not in toolbar_actions

    # Triggering "Reload plugins" asks the controller to re-scan the folder.
    view._pluginMenuActions["reload"].trigger()
    assert view.sigReloadPluginsRequested.emitted == [()]


def test_analysis_results_shortcut_raises_results_dock(qapp):
    class _Dock:
        def __init__(self):
            self.shown = False

        def show(self):
            self.shown = True

    view = QtWidgets.QMainWindow()
    view._processorToolbar = QtWidgets.QToolBar()
    view._analysisMenu = QtWidgets.QMenu()
    view._analysisToolActions = {}
    dock = _Dock()
    view.docks = {"Results": dock}
    view._safeRaiseDock = lambda current_dock: setattr(
        view,
        "raisedDock",
        current_dock,
    )
    view._syncDockVisibilityActions = lambda: setattr(view, "synced", True)
    view.raiseDockByTitle = (
        lambda title: ImProcessMainView.raiseDockByTitle(view, title)
    )

    action = ImProcessMainView._addAnalysisDockAction(
        view,
        "results-table",
        "Results",
        "Open the results table panel",
        "Results",
    )
    action.trigger()

    assert dock.shown is True
    assert view.raisedDock is dock
    assert view.synced is True


def test_register_processor_by_id_adds_builtin_processor():
    registry = PluginRegistry()

    plugin = register_processor_by_id(registry, "projection")

    assert plugin.id == "projection"
    assert registry.get_processor("projection") is plugin


def test_register_processor_by_id_rejects_unknown_id():
    registry = PluginRegistry()

    with pytest.raises(KeyError):
        register_processor_by_id(registry, "does-not-exist")


def test_configured_runtime_panel_registers_required_processor():
    registry = get_registry()
    registry.clear()
    view = SimpleNamespace(startupRuntimeAnalysisToolIds=lambda: ["frc"])
    controller = SimpleNamespace(
        _ImProcessMainController__mainView=view,
        _ImProcessMainController__logger=_Logger(),
    )

    try:
        ImProcessMainController._register_startup_runtime_processors(controller)

        assert registry.get_processor("frc", raise_on_missing=False) is not None
    finally:
        registry.clear()

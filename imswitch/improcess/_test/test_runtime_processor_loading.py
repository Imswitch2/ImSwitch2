import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qtpy import QtWidgets

from imswitch.improcess.processors import (
    available_processor_choices,
    available_processor_ids,
    register_processor_by_id,
)
from imswitch.improcess.model.runtime_tools import (
    runtime_analysis_panel_shortcuts,
    runtime_analysis_tool_specs,
)
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


def test_runtime_analysis_panel_shortcuts_cover_fiji_like_panels():
    shortcuts = runtime_analysis_panel_shortcuts()

    assert [shortcut.id for shortcut in shortcuts] == [
        "roi-manager",
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

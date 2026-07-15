"""Phase-2 toolbar reorg: origin-split enumerations and toolbar structure.

Locks in the split introduced by docs/improcess-image-ops-plan.md Phase 2:
Image (display-only) vs Image operations (result-producing), Tools (built-in
panels) vs Plugins (drop-in plugins), with origin-aware enumeration so a
hot-reloaded plugin lands in the Plugins combo and never leaks into Tools.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

import imswitch.improcess.processors as processors
from imswitch.improcess.controller.ImProcessMainController import (
    ImProcessMainController,
)
from imswitch.improcess.model.runtime_tools import (
    runtime_analysis_tool_choices,
    runtime_analysis_tool_specs,
)
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


_PLUGIN_SOURCE = """
from imswitch.improcess.model import ArrayProcessingResult
from imswitch.improcess.processors.base import Processor


class ReorgTestPlugin(Processor):
    name = "Reorg test plugin"
    id = "user.reorg-test"
    category = "Plugins"

    @property
    def applies_to(self):
        return lambda result: True

    def make_param_widget(self, parent):
        return None

    def apply(self, result, params):
        return ArrayProcessingResult(
            name=result.name, data=result.data, axis_labels=list(result.axis_labels)
        )
"""


@pytest.fixture
def user_plugin(tmp_path):
    plugin_dir = tmp_path / "improcess_plugins"
    plugin_dir.mkdir()
    (plugin_dir / "reorg_test.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    loaded, errors = processors.load_user_plugins(str(plugin_dir))
    assert loaded == ["user.reorg-test"] and errors == []
    yield "user.reorg-test"
    processors.clear_user_plugins()


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# -- origin-aware enumeration ------------------------------------------------

def test_available_processor_specs_split_by_origin(user_plugin):
    builtin_ids = {pid for pid, _n, _c in processors.available_processor_specs("builtin")}
    user_ids = {pid for pid, _n, _c in processors.available_processor_specs("user")}
    all_ids = {pid for pid, _n, _c in processors.available_processor_specs()}

    assert user_ids == {user_plugin}
    assert user_plugin not in builtin_ids
    assert "stack-combine" in builtin_ids
    assert all_ids == builtin_ids | user_ids
    with pytest.raises(ValueError):
        processors.available_processor_specs("nonsense")


def test_runtime_tool_specs_user_origin_has_no_builtin_panels(user_plugin):
    user_specs = runtime_analysis_tool_specs(origin="user")
    builtin_specs = runtime_analysis_tool_specs(origin="builtin")

    assert set(user_specs) == {user_plugin}
    # Non-processor panels (Graph/Profile/ROI) are built-in only.
    assert "graph" in builtin_specs and "graph" not in user_specs
    assert user_plugin not in builtin_specs
    # The merged view stays the union so load-by-id keeps working everywhere.
    assert set(runtime_analysis_tool_specs()) == set(builtin_specs) | set(user_specs)


def test_runtime_tool_choices_split_by_origin(user_plugin):
    user_choice_ids = [tool_id for tool_id, _t in runtime_analysis_tool_choices("user")]
    builtin_choice_ids = [
        tool_id for tool_id, _t in runtime_analysis_tool_choices("builtin")
    ]

    assert user_choice_ids == [user_plugin]
    assert user_plugin not in builtin_choice_ids
    assert "stack-combine" in builtin_choice_ids


# -- image toolbar split -------------------------------------------------------

class _RecordingImageView:
    """Records which action ids each image-toolbar builder registers."""

    def __init__(self):
        self.added: list[str] = []
        self._imageToolbar = SimpleNamespace(addSeparator=lambda: None)
        self._imageOpsToolbar = SimpleNamespace(addSeparator=lambda: None)
        self._imageMenu = SimpleNamespace(addSeparator=lambda: None)
        self._imageOpsMenu = SimpleNamespace(addSeparator=lambda: None)

    def __getattr__(self, name):
        if name.startswith("sigImage"):
            return object()
        raise AttributeError(name)

    def _addImageAction(self, action_id, _text, _tooltip, _icon, _signal, **_kwargs):
        self.added.append(action_id)

    def _addImageLutSelector(self):
        self.added.append("lut-selector")


def test_image_toolbar_holds_display_only_actions(qapp):
    view = _RecordingImageView()
    ImProcessMainView._buildImageToolbar(view)

    assert view.added == [
        "auto-contrast",
        "brightness-contrast",
        "reset-contrast",
        "lut-selector",
        "channels",
        "reset-view",
    ]


def test_image_ops_toolbar_holds_result_producing_actions(qapp):
    view = _RecordingImageView()
    ImProcessMainView._buildImageOpsToolbar(view)

    assert view.added == [
        "duplicate",
        "crop-substack",
        "max-projection",
        "split-stack",
        "split-channels",
        "merge-channels",
        "stack-combine",
        "make-composite",
        "make-rgb",
    ]


def test_ops_actions_land_on_the_ops_toolbar_and_menu(qapp):
    """The split is real: ops actions go to the ops toolbar/menu, not Image."""
    view = QtWidgets.QMainWindow()
    view._imageActions = {}
    view._imageToolbar = QtWidgets.QToolBar()
    view._imageOpsToolbar = QtWidgets.QToolBar()
    view._imageMenu = QtWidgets.QMenu()
    view._imageOpsMenu = QtWidgets.QMenu()

    class _Sig:
        def emit(self):
            pass

    icon = qapp.style().standardIcon(QtWidgets.QStyle.SP_FileIcon)
    action = ImProcessMainView._addImageAction(
        view,
        "duplicate",
        "Duplicate",
        "Duplicate the active result",
        icon,
        _Sig(),
        toolbar=view._imageOpsToolbar,
        menu=view._imageOpsMenu,
    )

    assert action in view._imageOpsToolbar.actions()
    assert action in view._imageOpsMenu.actions()
    assert action not in view._imageToolbar.actions()
    assert action not in view._imageMenu.actions()
    assert view._imageActions["duplicate"] is action


# -- plugins combo ---------------------------------------------------------------

class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def test_plugin_combo_populates_and_emits_load_request(qapp):
    view = QtWidgets.QMainWindow()
    view._loadPluginCombo = QtWidgets.QComboBox()
    view.sigLoadProcessorRequested = _Signal()

    ImProcessMainView.setAvailablePluginTools(
        view, [("user.reorg-test", "Reorg test plugin")]
    )
    assert view._loadPluginCombo.isEnabled()
    assert view._loadPluginCombo.count() == 2  # placeholder + plugin

    ImProcessMainView._on_load_plugin_combo_activated(view, 1)
    assert view.sigLoadProcessorRequested.emitted == [("user.reorg-test",)]
    # Selecting the placeholder emits nothing.
    ImProcessMainView._on_load_plugin_combo_activated(view, 0)
    assert view.sigLoadProcessorRequested.emitted == [("user.reorg-test",)]


def test_plugin_combo_disabled_with_placeholder_when_empty(qapp):
    view = QtWidgets.QMainWindow()
    view._loadPluginCombo = QtWidgets.QComboBox()

    ImProcessMainView.setAvailablePluginTools(view, [], placeholder="No plugins installed")

    assert not view._loadPluginCombo.isEnabled()
    assert view._loadPluginCombo.itemText(0) == "No plugins installed"


# -- controller refresh feeds both combos ------------------------------------------

def _refresh_capturing_controller():
    controller = ImProcessMainController.__new__(ImProcessMainController)
    captured = {}
    view = SimpleNamespace(
        isRuntimeAnalysisToolLoaded=lambda _tool_id: False,
        setAvailableRuntimeProcessors=lambda choices: captured.__setitem__(
            "builtin", choices
        ),
        setAvailablePluginTools=lambda choices, placeholder=None: captured.update(
            plugins=choices, plugin_placeholder=placeholder
        ),
        setLoadedRuntimeProcessors=lambda choices: captured.__setitem__(
            "loaded", choices
        ),
    )
    controller._ImProcessMainController__mainView = view
    return controller, captured


def test_controller_refresh_splits_builtin_and_plugin_choices(user_plugin):
    controller, captured = _refresh_capturing_controller()

    controller._refresh_runtime_processor_choices()

    builtin_ids = [tool_id for tool_id, _t in captured["builtin"]]
    plugin_ids = [tool_id for tool_id, _t in captured["plugins"]]
    assert user_plugin in plugin_ids
    assert user_plugin not in builtin_ids
    assert "stack-combine" in builtin_ids
    assert captured["plugin_placeholder"] == "All plugins loaded"


def test_controller_refresh_reports_no_plugins_installed():
    processors.clear_user_plugins()
    controller, captured = _refresh_capturing_controller()

    controller._refresh_runtime_processor_choices()

    assert captured["plugins"] == []
    assert captured["plugin_placeholder"] == "No plugins installed"

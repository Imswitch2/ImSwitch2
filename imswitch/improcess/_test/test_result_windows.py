"""Window mode: the preference, the manager, the routing and the Window menu.

Real pyqtgraph windows offscreen; the napari view is never built (it crashes
offscreen), so the controller runs against a stub list pane.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.display_preferences import (
    IMAGE_DISPLAY_NAPARI,
    IMAGE_DISPLAY_WINDOWS,
    DisplayPreferences,
    load_display_preferences,
    save_display_preferences,
)
from imswitch.improcess.processors.make_composite.result import CompositeResult
from imswitch.improcess.view import window_mode
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView
from imswitch.improcess.view.ReconstructionView import ReconstructionView
from imswitch.improcess.view.result_windows import (
    ResultWindowManager,
    cascade_geometry,
    has_window_image,
    tile_geometry,
)


def _result(name, shape=(12, 16), labels="YX"):
    data = np.random.default_rng(len(name)).integers(0, 255, shape).astype(np.uint16)
    return ArrayProcessingResult(name, data, list(labels))


# --- the preference ---------------------------------------------------------


def test_the_preference_defaults_to_napari(tmp_path):
    assert load_display_preferences(tmp_path / "missing.json").imageDisplay == IMAGE_DISPLAY_NAPARI


def test_the_preference_round_trips_and_keeps_unknown_keys(tmp_path):
    path = tmp_path / "improcess_options.json"
    path.write_text(json.dumps({"fijiDir": "/opt/fiji", "future": {"a": 1}}))
    save_display_preferences(DisplayPreferences(IMAGE_DISPLAY_WINDOWS), path)
    assert load_display_preferences(path).imageDisplay == IMAGE_DISPLAY_WINDOWS
    content = json.loads(path.read_text())
    assert content["fijiDir"] == "/opt/fiji" and content["future"] == {"a": 1}
    assert content["imageDisplay"] == "windows"


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"imageDisplay": "bogus"}', '{"imageDisplay": 3}'])
def test_an_unusable_preference_means_napari(tmp_path, text):
    path = tmp_path / "improcess_options.json"
    path.write_text(text)
    assert load_display_preferences(path).imageDisplay == IMAGE_DISPLAY_NAPARI


def test_saving_over_a_corrupt_file_replaces_it(tmp_path):
    path = tmp_path / "improcess_options.json"
    path.write_text("{broken")
    save_display_preferences(DisplayPreferences(IMAGE_DISPLAY_WINDOWS), path)
    assert load_display_preferences(path).imageDisplay == IMAGE_DISPLAY_WINDOWS


# --- the manager ------------------------------------------------------------


@pytest.fixture
def manager(qtbot):
    parent = QtWidgets.QWidget()
    qtbot.addWidget(parent)
    messages = []
    manager = ResultWindowManager(parent, statusMessage=messages.append)
    manager.messages = messages
    yield manager
    manager.closeAll()


def test_one_window_per_result_and_reuse_on_reselect(manager):
    a, b = _result("a"), _result("b")
    wa = manager.show(a)
    wb = manager.show(b)
    assert wa is not wb and len(manager.windows()) == 2
    assert manager.show(a) is wa and len(manager.windows()) == 2
    assert manager.activeWindow() is wa
    assert wa.windowTitle() == "a"


def test_closing_hides_and_keeps_the_window_for_reopening(manager):
    a = _result("a")
    window = manager.show(a)
    window.close()
    assert not window.isVisible()
    assert manager.windowFor(a) is window
    assert manager.activeWindow() is None
    assert manager.show(a) is window and window.isVisible()


def test_removing_a_result_disposes_its_window(manager):
    a, b = _result("a"), _result("b")
    manager.show(a)
    wb = manager.show(b)
    manager.retainOnly([b])
    assert manager.windowFor(a) is None
    assert manager.windows() == [wb]


def test_results_without_an_image_open_no_window_and_say_so(manager):
    table = SimpleNamespace(name="t", kind="table", display_layers=lambda: [])
    assert not has_window_image(table)
    assert manager.show(table) is None
    assert manager.windows() == []
    assert "no image to show in a window" in manager.messages[0]


def test_replace_points_the_window_at_the_new_result(manager):
    old, new = _result("old"), _result("new")
    window = manager.show(old)
    assert manager.replace(old, new)
    assert manager.windowFor(new) is window and manager.windowFor(old) is None
    assert window.result is new and window.windowTitle() == "new"


def test_a_user_activation_is_reported_once_and_a_programmatic_one_not(manager, qtbot):
    a, b = _result("a"), _result("b")
    wa = manager.show(a)
    wb = manager.show(b)
    heard = []
    manager.sigWindowActivated.connect(heard.append)

    manager.show(a)  # programmatic raise: the list is already on 'a'
    assert heard == []
    wb.sigActivated.emit()  # the user clicks b
    assert heard == [b]
    wb.sigActivated.emit()  # b is already active: nothing more
    assert heard == [b]
    assert manager.activeWindow() is wb and wa is not wb


# --- the controller routing ---------------------------------------------------


class _ListPane(QtCore.QObject):
    """The slice of ReconstructionView the controller touches in window mode."""

    sigItemSelected = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.reconList = QtWidgets.QListWidget()
        self.reconList.currentItemChanged.connect(self.sigItemSelected)
        self.napariCalls = []
        self.napariStormDisplay = None

    def add(self, result):
        item = QtWidgets.QListWidgetItem(result.name)
        item.setData(1, result)
        self.reconList.addItem(item)
        self.reconList.setCurrentItem(item)

    def getCurrentItemData(self):
        item = self.reconList.currentItem()
        return item.data(1) if item is not None else None

    def getCurrentItemIndex(self):
        return self.reconList.currentRow()

    def getAllItemDatas(self):
        return [(self.reconList.item(i).text(), self.reconList.item(i).data(1))
                for i in range(self.reconList.count())]

    def getViewName(self):
        return "standard"

    def setViewModes(self, modes):
        pass

    def resetView(self):
        self.napariCalls.append("resetView")

    def getActiveImageColormap(self):
        self.napariCalls.append("getActiveImageColormap")
        return "napari-colormap"

    def __getattr__(self, name):  # any other napari call is recorded, never executed
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self.napariCalls.append(name)


def _controller(pane, manager=None):
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = pane
    emitted = []
    controller._commChannel = SimpleNamespace(
        sigCurrentResultChanged=SimpleNamespace(emit=emitted.append),
        sigResultsChanged=SimpleNamespace(emit=lambda: None),
    )
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._currItemInd = None
    controller._prevViewId = None
    controller._displayedResult = None
    controller._emitted = emitted
    pane.sigItemSelected.connect(lambda: controller.listItemChanged())
    if manager is not None:
        controller.setWindowManager(manager)
        # The controller here is built without its QObject __init__, so Qt does
        # not deliver to its bound methods; route the one signal by hand.
        manager.sigWindowActivated.connect(lambda result: controller._windowActivated(result))
    return controller


@pytest.fixture
def windowed(manager):
    pane = _ListPane()
    controller = _controller(pane, manager)
    return pane, controller, manager


def test_selecting_a_result_opens_and_raises_its_window(windowed):
    pane, controller, manager = windowed
    a, b = _result("a"), _result("b")
    pane.add(a)
    pane.add(b)
    assert len(manager.windows()) == 2
    assert manager.activeWindow() is manager.windowFor(b)
    pane.reconList.setCurrentRow(0)
    assert manager.activeWindow() is manager.windowFor(a)
    assert controller._emitted[-1] is a
    assert pane.napariCalls == []  # the napari display was never touched


def test_activating_a_window_selects_its_row_without_a_loop(windowed, monkeypatch):
    pane, controller, manager = windowed
    a, b = _result("a"), _result("b")
    pane.add(a)
    pane.add(b)  # b is current and active
    shows, renders = [], []
    real_show = manager.show
    monkeypatch.setattr(manager, "show", lambda *a_, **k: (shows.append(k), real_show(*a_, **k))[1])
    real_update = controller.fullUpdate
    monkeypatch.setattr(controller, "fullUpdate", lambda *a_, **k: (renders.append(1), real_update(*a_, **k))[1])

    manager.windowFor(a).sigActivated.emit()  # the user clicks window a

    assert pane.reconList.currentRow() == 0 and controller.getActiveResult() is a
    assert renders == [1]  # one render, not a ping-pong
    assert shows == [{"activate": False}]  # it followed the window; it did not raise it again
    assert manager.activeWindow() is manager.windowFor(a)
    assert controller._emitted[-1] is a  # the toolbar is told the current result moved


def test_the_toolbar_acts_on_the_active_window(windowed):
    pane, controller, manager = windowed
    a = CompositeResult("a", np.arange(2 * 8 * 8, dtype=np.uint16).reshape(2, 8, 8),
                        ["C", "Y", "X"], channel_axis=0, channel_colormaps=["red", "green"])
    b = _result("b")
    pane.add(a)
    pane.add(b)
    window_a, window_b = manager.windowFor(a), manager.windowFor(b)

    controller.setActiveImageDisplayLevels(3, 30)  # active window: b
    controller.setActiveImageColormap("cyan")
    assert window_b.getActiveImageDisplayLevels() == (3.0, 30.0)
    assert b.getDispLevels() == (3.0, 30.0) and b.getDisplayColormap() == "cyan"
    assert window_a.getActiveImageColormap() == "red"
    assert controller.getActiveImageDisplayLevels() == (3.0, 30.0)
    assert controller.getActiveImageColormap() == "cyan"

    pane.reconList.setCurrentRow(0)  # window a becomes the active one
    assert [s["id"] for s in controller.getDisplayLayerStates()] == ["C_0", "C_1"]
    controller.setDisplayLayerColormap("C_1", "magenta")
    controller.setDisplayLayerVisible("C_0", False)
    assert a.getDisplayLayerColormap("C_1") == "magenta"
    assert a.getDisplayLayerVisible("C_0") is False
    assert np.array_equal(controller.getActiveImage(), window_a.getActiveImage())
    assert controller.getActiveImageCurrentView() is window_a.currentPlane()
    assert pane.napariCalls == []


def test_with_no_open_window_the_toolbar_does_nothing_and_says_nothing_false(windowed):
    pane, controller, manager = windowed
    a = _result("a")
    pane.add(a)
    manager.windowFor(a).close()
    controller.setActiveImageDisplayLevels(1, 2)
    controller.setActiveImageColormap("red")
    assert controller.getActiveImage() is None
    assert controller.getActiveImageDisplayLevels() is None
    assert controller.getDisplayLayerStates() == []
    assert a.getDispLevels() is None


def test_double_clicking_a_row_reopens_its_window(windowed):
    pane, controller, manager = windowed
    a = _result("a")
    pane.add(a)
    window = manager.windowFor(a)
    window.close()
    controller._listItemDoubleClicked(pane.reconList.item(0))
    assert window.isVisible() and manager.activeWindow() is window


def test_removing_a_result_from_the_list_closes_its_window(windowed):
    pane, controller, manager = windowed
    a, b = _result("a"), _result("b")
    pane.add(a)
    pane.add(b)
    pane.reconList.takeItem(0)
    controller._resultsChanged()
    assert manager.windowFor(a) is None and manager.windowFor(b) is not None


def test_napari_mode_is_unchanged_without_a_manager():
    pane = _ListPane()
    controller = _controller(pane)
    assert controller._windowManager() is None
    assert controller.getActiveImageColormap() == "napari-colormap"
    controller.resetView()
    assert pane.napariCalls == ["getActiveImageColormap", "resetView"]
    controller.setActiveImageDisplayLevels(1, 2)
    assert "setActiveImageDisplayLevels" in pane.napariCalls


# --- the Window menu and geometry ---------------------------------------------


def test_the_window_menu_lists_open_windows_tile_and_cascade(manager, qtbot):
    menu = QtWidgets.QMenu()
    manager.setWindowMenu(menu)
    assert [a.text() for a in menu.actions() if a.text()] == [
        "Tile", "Cascade", "(no image windows open)"]
    a, b = _result("alpha"), _result("beta")
    manager.show(a)
    wb = manager.show(b)
    wb.close()
    manager.rebuildWindowMenu()
    texts = [action.text() for action in menu.actions() if action.text()]
    assert texts == ["Tile", "Cascade", "alpha"]  # the hidden one is not listed
    alpha = [action for action in menu.actions() if action.text() == "alpha"][0]
    heard = []
    manager.sigWindowActivated.connect(heard.append)
    manager.show(b)
    manager.rebuildWindowMenu()
    alpha = [action for action in menu.actions() if action.text() == "alpha"][0]
    alpha.trigger()  # clicking raises it and the list follows
    assert manager.activeWindow() is manager.windowFor(a)
    assert heard == [a]


def test_tile_and_cascade_geometry_stay_inside_the_area():
    area = QtCore.QRect(10, 20, 1000, 600)
    for count in (1, 2, 3, 5, 9):
        cells = tile_geometry(count, area)
        assert len(cells) == count
        assert all(area.contains(cell) for cell in cells)
        for i, first in enumerate(cells):
            assert all(not first.intersects(second) for second in cells[i + 1:])
    sizes = [QtCore.QSize(400, 300)] * 40 + [QtCore.QSize(5000, 5000)]
    rects = cascade_geometry(sizes, area, 24)
    assert all(area.contains(rect) for rect in rects)
    assert rects[1].topLeft() - rects[0].topLeft() == QtCore.QPoint(24, 24)


def test_tile_and_cascade_move_real_windows_onto_the_screen(manager):
    for name in ("a", "b", "c"):
        manager.show(_result(name))
    area = manager.availableArea()
    manager.tile()
    assert all(area.contains(window.geometry()) for window in manager.openWindows())
    manager.cascade()
    assert all(area.contains(window.geometry()) for window in manager.openWindows())


# --- what is not supported yet -------------------------------------------------


def test_the_unsupported_registry_names_the_napari_only_tools():
    ids = {tool.id for tool in window_mode.unsupported_tools()}
    assert ids == {"segmentation", "psf-resolution", "colocalization", "multicolor",
                   "napari-plugins"}
    assert "separate windows" in window_mode.UNSUPPORTED_TOOLTIP


def test_a_tool_marked_supported_is_not_restricted(monkeypatch):
    monkeypatch.setattr(window_mode, "SUPPORTED_IN_WINDOW_MODE", frozenset({"profile"}))
    assert window_mode.panel_tool("profile") is None
    assert window_mode.panel_tool("roi-manager") is not None


def _stub_main_view(qtbot):
    panels = {tool.attribute: QtWidgets.QWidget() for tool in window_mode.NAPARI_ONLY_TOOLS
              if tool.kind == "panel"}
    for widget in panels.values():
        qtbot.addWidget(widget)
    menu_owner = QtWidgets.QMenu("Window")
    plugins = QtWidgets.QMenu("napari plugins")
    actions = {"crop-substack": QtWidgets.QAction("Crop"), "duplicate": QtWidgets.QAction("Dup")}
    canvas = []
    view = SimpleNamespace(
        windowMenu=menu_owner, _napariPluginsMenu=plugins, _imageActions=actions,
        _blockedImageActions=set(), reconstructionWidget=SimpleNamespace(
            setCanvasVisible=canvas.append),
        setImageDisplayChoiceChecked=lambda mode: None, canvas=canvas, **panels,
    )
    return view


def test_window_mode_disables_the_napari_only_tools_with_a_tooltip(qtbot, monkeypatch):
    # Crop/Substack is supported now; an image action is still blocked the same way
    # when a tool of that kind is not (this is the machinery the next one will use).
    monkeypatch.setattr(window_mode, "SUPPORTED_IN_WINDOW_MODE", frozenset({"profile"}))
    view = _stub_main_view(qtbot)
    ImProcessMainView.setImageDisplayMode(view, IMAGE_DISPLAY_WINDOWS)
    assert view.canvas == [False]
    for tool in window_mode.unsupported_tools():
        if tool.kind == "panel":
            widget = getattr(view, tool.attribute)
            assert not widget.isEnabled() and widget.toolTip() == window_mode.UNSUPPORTED_TOOLTIP
    crop = view._imageActions["crop-substack"]
    assert not crop.isEnabled() and crop.toolTip() == window_mode.UNSUPPORTED_TOOLTIP
    assert not view._napariPluginsMenu.menuAction().isEnabled()
    # The toolbar controller re-enabling actions per result cannot override it.
    ImProcessMainView.setImageActionEnabled(view, "crop-substack", True)
    ImProcessMainView.setImageActionsEnabled(view, True) if hasattr(view, "setImageLutEnabled") else None
    assert not crop.isEnabled()
    ImProcessMainView.setImageActionEnabled(view, "duplicate", True)
    assert view._imageActions["duplicate"].isEnabled()


def test_napari_mode_restricts_nothing(qtbot):
    view = _stub_main_view(qtbot)
    ImProcessMainView.setImageDisplayMode(view, IMAGE_DISPLAY_NAPARI)
    assert view.canvas == [True]
    assert all(getattr(view, t.attribute).isEnabled()
               for t in window_mode.NAPARI_ONLY_TOOLS if t.kind == "panel")
    assert view._blockedImageActions == set()


def test_the_view_can_hide_the_canvas_and_keep_the_list():
    assert callable(ReconstructionView.setCanvasVisible)


def test_the_toolbar_resets_the_view_through_the_controller():
    from imswitch.improcess.controller.ImageToolbarController import ImageToolbarController

    calls = []
    toolbar = ImageToolbarController.__new__(ImageToolbarController)
    toolbar._reconstructionController = SimpleNamespace(resetView=lambda: calls.append("controller"))
    toolbar._view = SimpleNamespace(reconstructionWidget=SimpleNamespace(
        resetView=lambda: calls.append("widget")))
    toolbar._logger = SimpleNamespace(exception=lambda *a, **k: None)
    toolbar.resetView()
    assert calls == ["controller"]


def test_windows_are_tool_windows_owned_by_the_main_window(qtbot):
    """Owned tool windows stay above the main window when the toolbar is clicked."""
    from qtpy import QtCore, QtWidgets
    import numpy as np

    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.view.result_windows import ResultWindowManager

    main = QtWidgets.QMainWindow()
    qtbot.addWidget(main)
    manager = ResultWindowManager(main)
    window = manager.show(ArrayProcessingResult("r", np.zeros((8, 8), np.float32), ["Y", "X"]))
    assert window.parentWidget() is main
    assert window.windowType() == QtCore.Qt.Tool
    unowned = ResultWindowManager(None).show(ArrayProcessingResult("u", np.zeros((8, 8), np.float32), ["Y", "X"]))
    qtbot.addWidget(unowned)
    assert unowned.windowType() == QtCore.Qt.Window
    manager.closeAll()


def test_an_owned_window_stays_visible_while_another_application_is_active(qtbot):
    """A macOS tool window hides with its application unless it asks not to."""
    from qtpy import QtCore, QtWidgets
    import numpy as np

    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.view.result_windows import ResultWindowManager

    main = QtWidgets.QMainWindow()
    qtbot.addWidget(main)
    manager = ResultWindowManager(main)
    owned = manager.show(ArrayProcessingResult("r", np.zeros((8, 8), np.float32), ["Y", "X"]))
    assert owned.testAttribute(QtCore.Qt.WA_MacAlwaysShowToolWindow)

    unowned = ResultWindowManager(None).show(ArrayProcessingResult("u", np.zeros((8, 8), np.float32), ["Y", "X"]))
    qtbot.addWidget(unowned)
    assert unowned.windowType() == QtCore.Qt.Window
    assert not unowned.testAttribute(QtCore.Qt.WA_MacAlwaysShowToolWindow)
    manager.closeAll()


# --------------------------------------------------------------------------
# shutdown: ImProcessMainView is a tab page and never gets a closeEvent, so the
# controller's own closeEvent (called by the module shutdown) must close the windows
# --------------------------------------------------------------------------

def _bare_main_controller(manager):
    from types import SimpleNamespace

    from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController

    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__guiLayoutStateAdapter = None
    controller._ImProcessMainController__logger = SimpleNamespace(warning=lambda *a, **k: None)
    calls = []
    controller._ImProcessMainController__factory = SimpleNamespace(
        closeAllCreatedControllers=lambda waitTimeoutS=None: calls.append("controllers"))
    controller.resultWindowManager = manager
    return controller, calls


def test_the_controllers_close_event_closes_the_image_windows_before_the_controllers(qtbot):
    from types import SimpleNamespace

    order = []
    manager = SimpleNamespace(closeAll=lambda: order.append("windows"))
    controller, _calls = _bare_main_controller(manager)
    controller._ImProcessMainController__factory.closeAllCreatedControllers = (
        lambda waitTimeoutS=None: order.append("controllers"))

    controller.closeEvent()

    assert order == ["windows", "controllers"]


def test_the_controllers_close_event_without_window_mode_or_with_a_failing_manager(qtbot):
    from types import SimpleNamespace

    controller, calls = _bare_main_controller(None)       # the default napari display
    controller.closeEvent()
    assert calls == ["controllers"]

    def boom():
        raise RuntimeError("wrapped C/C++ object of type ImageWindow has been deleted")

    controller, calls = _bare_main_controller(SimpleNamespace(closeAll=boom))
    controller.closeEvent()                                # the controllers still close
    assert calls == ["controllers"]


def test_close_all_survives_windows_qt_already_deleted(qtbot):
    import numpy as np
    from qtpy import QtWidgets
    try:
        import sip
    except ImportError:  # pragma: no cover
        from PyQt5 import sip

    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.view.result_windows import ResultWindowManager

    main = QtWidgets.QMainWindow()
    qtbot.addWidget(main)
    manager = ResultWindowManager(main)
    first = manager.show(ArrayProcessingResult("a", np.zeros((8, 8), np.float32), ["Y", "X"]))
    second = manager.show(ArrayProcessingResult("b", np.zeros((8, 8), np.float32), ["Y", "X"]))
    sip.delete(first)                      # Qt took the first one down with its owner

    manager.closeAll()                     # must not raise, and must still dispose the second

    assert manager.windows() == []
    assert sip.isdeleted(first) and (sip.isdeleted(second) or not second.isVisible())
    manager.closeAll()                     # and a second call is harmless

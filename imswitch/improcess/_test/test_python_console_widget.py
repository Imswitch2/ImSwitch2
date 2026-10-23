"""The console dock offscreen: the editor and the REPL share one namespace, the
controller keeps it following the results list, and the code goes to the step."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import output_node  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def widget(qapp):
    from imswitch.improcess.view.PythonConsoleWidget import PythonConsoleWidget

    return PythonConsoleWidget()


def _pane(widget):
    return widget.console.output.toPlainText()


def _stack(name="rec", count=6):
    data = np.broadcast_to(np.arange(count, dtype=np.float32)[:, None, None], (count, 4, 4))
    return ArrayProcessingResult(name, np.array(data), ["Z", "Y", "X"])


# -- the widget ---------------------------------------------------------------

def test_the_editor_and_the_repl_run_in_one_namespace(widget):
    space = widget.namespace()
    assert space is widget.console.localNamespace and space["__console__"] is widget.console
    widget.editor.setText("x = 6\ny = x * 7\n")
    assert widget.runEditor() is True
    assert space["y"] == 42
    widget.console.repl.handleCommand("z = y + 1")             # a one-line command, as typed
    assert space["z"] == 43
    widget.editor.setText("print(z)")
    widget.runEditor()
    assert "43" in _pane(widget)


def test_what_the_editor_code_prints_goes_to_the_console_and_not_the_terminal(widget, capsys):
    widget.editor.setText('print("hello from the editor")\nprint("no newline", end="")')
    assert widget.runEditor() is True
    text = _pane(widget)
    assert "# editor: 2 lines" in text
    assert "hello from the editor" in text and "no newline" in text
    assert "hello from the editor" not in capsys.readouterr().out


def test_run_uses_the_selected_lines_when_there_are_some(widget, monkeypatch):
    widget.editor.setText("first = 1\nsecond = 2\n")
    monkeypatch.setattr(widget.editor, "selectedText", lambda: "second = 2")
    widget.runEditor()
    space = widget.namespace()
    assert space.get("second") == 2 and "first" not in space


def test_an_error_is_shown_with_its_line_and_the_console_stays_usable(widget):
    widget.editor.setText('print("before")\nvalue = undefined_name\nprint("after")\n')
    assert widget.runEditor() is False
    text = _pane(widget)
    assert "before" in text and "after" not in text
    assert 'File "<console editor>", line 2' in text and "NameError: name 'undefined_name'" in text
    widget.editor.setText("recovered = True")
    assert widget.runEditor() is True and widget.namespace()["recovered"] is True


def test_exit_in_editor_code_is_an_error_not_the_end_of_the_program(widget):
    widget.editor.setText("raise SystemExit(0)")
    assert widget.runEditor() is False
    assert "SystemExit" in _pane(widget)


def test_a_blank_editor_runs_nothing(widget):
    before = _pane(widget)
    widget.editor.setText("  \n")
    assert widget.runEditor() is True and _pane(widget) == before


def test_the_starter_text_is_valid_python_that_does_nothing_but_comment():
    from imswitch.improcess.view.PythonConsoleWidget import STARTER_TEXT

    compile(STARTER_TEXT, "<starter>", "exec")
    assert all(line.startswith("#") for line in STARTER_TEXT.strip().splitlines())


def test_a_command_announces_itself_before_it_runs(widget):
    seen = []
    widget.sigCommandStarting.connect(lambda: seen.append(widget.namespace().get("probe")))
    widget.namespace()["probe"] = "before"
    widget.console.repl.handleCommand("probe = 'after'")
    assert seen == ["before"] and widget.namespace()["probe"] == "after"
    widget.editor.setText("probe = 'again'")
    widget.runEditor()
    assert seen == ["before", "after"]


def test_send_to_python_step_hands_over_the_editors_text_and_nothing_when_blank(widget):
    sent = []
    widget.sigSendToPythonStep.connect(sent.append)
    widget.editor.setText("out = data * 2\n")
    assert widget.sendToPythonStep() is True and sent == ["out = data * 2\n"]
    widget.editor.setText(" \n")
    assert widget.sendToPythonStep() is False and len(sent) == 1
    widget.editor.setText("a = 1")
    widget.sendButton.click()
    assert sent[-1] == "a = 1"


# -- the controller ------------------------------------------------------------

class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _Comm:
    def __init__(self, selected=()):
        self.selected = list(selected)
        self.sigCurrentResultChanged = _Signal()
        self.sigResultsChanged = _Signal()
        self.sigResultProduced = _Signal()
        self.produced = []
        self.sigResultProduced.connect(lambda result, name: self.produced.append((result, name)))

    def getSelectedResults(self):
        return [(getattr(r, "name", "r"), r) for r in self.selected]


def _bound_to(value, result) -> bool:
    """``value`` shows ``result``'s pixels, read-only: the namespace never hands
    out a writable input, so identity with ``result.data`` is not the test."""
    return (
        isinstance(value, np.ndarray)
        and np.shares_memory(value, result.data)
        and not value.flags.writeable
    )


def _controller(widget, comm):
    from imswitch.improcess.controller.PythonConsoleController import PythonConsoleController

    return PythonConsoleController(commChannel=comm, widget=widget, factory=None, moduleCommChannel=None)


def test_the_controller_binds_the_namespace_to_the_selection_and_follows_it(widget):
    a, b = _stack("a"), _stack("b", count=3)
    comm = _Comm([a])
    _controller(widget, comm)
    space = widget.namespace()
    assert _bound_to(space["data"], a) and space["results"] == [a]

    comm.selected = [a, b]
    comm.sigResultsChanged.emit()                              # the list announces a new selection
    assert space["results"] == [a, b] and [x.shape[0] for x in space["inputs"]] == [6, 3]

    comm.selected = []
    current = _stack("shown")
    comm.sigCurrentResultChanged.emit(current)                 # nothing selected: follow the current one
    assert _bound_to(space["data"], current)


def test_the_controller_can_be_told_the_current_result_when_the_console_opens_late(widget):
    comm = _Comm([])
    controller = _controller(widget, comm)
    assert widget.namespace()["data"] is None
    shown = _stack("shown")
    controller.seed(shown)
    assert _bound_to(widget.namespace()["data"], shown)
    assert controller.session.current() is shown


def test_a_selection_moved_without_a_signal_is_picked_up_before_the_next_command(widget):
    a, b = _stack("a"), _stack("b")
    comm = _Comm([a])
    _controller(widget, comm)
    comm.selected = [b]                                        # no signal reached the console
    widget.editor.setText("seen = data")
    widget.runEditor()
    assert _bound_to(widget.namespace()["seen"], b)


def test_the_users_own_names_survive_until_the_selection_really_changes(widget):
    a = _stack("a")
    comm = _Comm([a])
    _controller(widget, comm)
    widget.editor.setText("data = data[:2]\nmine = 7")
    widget.runEditor()
    widget.editor.setText("kept = data.shape[0]")
    widget.runEditor()
    assert widget.namespace()["kept"] == 2 and widget.namespace()["mine"] == 7   # data was not rebound
    comm.selected = [_stack("b")]
    comm.sigResultsChanged.emit()
    assert widget.namespace()["data"].shape[0] == 6 and widget.namespace()["mine"] == 7


def test_publish_from_the_editor_adds_to_the_list_and_records_an_opaque_node(widget):
    a = _stack("a")
    comm = _Comm([a])
    _controller(widget, comm)
    widget.editor.setText('first = publish(data * 2, name="doubled")\nsecond = publish(data + 1)\n')
    assert widget.runEditor() is True
    assert [name for _r, name in comm.produced] == ["doubled", "a (console)"]
    doubled = comm.produced[0][0]
    assert np.array_equal(doubled.data, a.data * 2) and doubled.axis_labels == ["Z", "Y", "X"]
    node = output_node(doubled)
    assert node["op"] == "opaque" and node["console"] is True and node["replayable"] is False
    assert np.array_equal(comm.produced[1][0].data, a.data + 1)       # still derived from "a", not from "doubled"


def test_a_failed_publish_shows_in_the_console_and_adds_nothing(widget):
    comm = _Comm([_stack("a")])
    _controller(widget, comm)
    widget.editor.setText("publish(data.max(axis=0))")
    assert widget.runEditor() is False
    assert "has 2 dimensions but the input has 3" in _pane(widget) and comm.produced == []


# -- wiring into ImProcess ------------------------------------------------------

def test_the_console_is_a_runtime_tool_of_the_scripting_category():
    from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs, runtime_result_processor_ids

    spec = runtime_analysis_tool_specs()["console"]
    assert (spec.title, spec.widget_kind, spec.processor_id, spec.category) == ("Console", "console", None, "Scripting")
    assert "console" not in runtime_result_processor_ids()          # it runs no processor


def test_the_main_view_builds_the_console_on_demand(qapp):
    from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView
    from imswitch.improcess.view.PythonConsoleWidget import PythonConsoleWidget

    view = SimpleNamespace(reconstructionWidget=SimpleNamespace(napariViewer=None), roiManagerWidget=None)
    factory = ImProcessMainView._runtimeAnalysisToolFactory(view, runtime_analysis_tool_specs()["console"])
    assert isinstance(factory(), PythonConsoleWidget)


class _MainView:
    def __init__(self, widgets):
        self.widgets = widgets

    def getRuntimeAnalysisWidget(self, tool_id):
        return self.widgets.get(tool_id)


def _main_controller(widgets, comm, current):
    from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
    from imswitch.improcess.controller.PythonConsoleController import PythonConsoleController

    class Factory:
        def createController(self, controller_cls, widget):
            assert controller_cls is PythonConsoleController
            return PythonConsoleController(commChannel=comm, widget=widget, factory=None, moduleCommChannel=None)

    statuses = []
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = _MainView(widgets)
    controller._ImProcessMainController__factory = Factory()
    controller._resultProcessorControllers = {}
    controller._consoleController = None
    controller.mainViewController = SimpleNamespace(
        reconstructionController=SimpleNamespace(getActiveResult=lambda: current)
    )
    controller._show_status_message = statuses.append
    return controller, statuses


def test_opening_the_console_wires_it_to_the_list_with_the_current_result(widget):
    shown = _stack("shown")
    comm = _Comm([])
    controller, _ = _main_controller({"console": widget}, comm, shown)
    controller._wire_runtime_result_processor("console")
    assert _bound_to(widget.namespace()["data"], shown)
    first = controller._consoleController
    controller._wire_runtime_result_processor("console")          # wiring again does not stack a second controller
    assert controller._consoleController is first
    comm.selected = [_stack("picked")]
    comm.sigResultsChanged.emit()
    assert widget.namespace()["results"][0].name == "picked"


def test_send_to_python_step_opens_the_step_and_fills_its_editor_without_running_it(widget, qapp):
    from imswitch.improcess.processors.python_step import PythonStepProcessor
    from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget

    panel = ResultProcessorWidget(PythonStepProcessor())
    comm = _Comm([_stack("a")])
    controller, statuses = _main_controller({"console": widget, "python": panel}, comm, None)
    opened = []
    controller._load_runtime_processor = opened.append
    controller._wire_runtime_result_processor("console")
    widget.editor.setText("x = data[::2]\npublish(x)\n")
    widget.sendButton.click()                                      # the widget's signal reaches the controller
    assert opened == ["python"]
    assert panel.parameterValues()["code"] == "x = data[::2]\npublish(x)\n"
    assert panel.parameterValues()["ports"] == "out"                # the ports are left as they were
    assert "nothing has run yet" in panel.statusLabel.text()
    assert "outputs = {...} instead of publish(...)" in panel.statusLabel.text()
    assert comm.produced == [] and statuses == ["Code sent to the Python step."]

    widget.editor.setText("outputs = {'out': data}\n")
    widget.sendButton.click()
    assert "publish" not in panel.statusLabel.text()                # the hint only when the code publishes


def test_send_to_python_step_says_so_when_the_step_cannot_be_opened(widget):
    comm = _Comm([])
    controller, statuses = _main_controller({"console": widget}, comm, None)
    controller._load_runtime_processor = lambda tool: None
    controller._wire_runtime_result_processor("console")
    widget.editor.setText("x = 1")
    widget.sendButton.click()
    assert statuses == ["Could not open the Python step."]


def test_a_panel_only_takes_values_its_parameter_widget_lets_be_set(qapp):
    from imswitch.improcess.processors.python_step import PythonStepProcessor
    from imswitch.improcess.processors.filters import FilterProcessor
    from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget

    step = ResultProcessorWidget(PythonStepProcessor())
    assert step.setParameterValues({"code": "a = 1\n", "ports": "p, q"}) is True
    assert step.parameterValues() == {"code": "a = 1\n", "ports": "p, q"}
    assert step.setParameterValues({"code": "b = 2\n"}) is True
    assert step.parameterValues() == {"code": "b = 2\n", "ports": "p, q"}       # a key not given is left alone
    assert ResultProcessorWidget(FilterProcessor()).setParameterValues({"radius": 3}) is False

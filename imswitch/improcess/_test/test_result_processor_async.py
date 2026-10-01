"""A panel's run happens off the GUI thread: the window stays alive, Cancel
works, output streams into the pane, and the results are published when it ends.
Real widget, real controller, real threads (offscreen)."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.processors.python_step import PythonStepProcessor  # noqa: E402
from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget  # noqa: E402


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _Comm:
    def __init__(self):
        self.sigCurrentResultChanged = _Signal()
        self.sigResultsChanged = _Signal()
        self.sigResultProduced = _Signal()
        self.produced = []
        self.sigResultProduced.connect(lambda result, name: self.produced.append((result, name)))

    def getAllResults(self):
        return []

    def getSelectedResults(self):
        return []


def _stack(name="rec", count=6):
    data = np.broadcast_to(np.arange(count, dtype=np.float32)[:, None, None], (count, 4, 4))
    return ArrayProcessingResult(name, np.array(data), ["Z", "Y", "X"])


@pytest.fixture
def panel(qtbot):
    widget = ResultProcessorWidget(PythonStepProcessor())
    comm = _Comm()
    controller = ResultProcessorController(commChannel=comm, widget=widget, factory=None, moduleCommChannel=None)
    stack = _stack()
    widget.setAvailableResults([("rec", stack)], [stack])
    widget.setCurrentResult(stack)
    qtbot.addWidget(widget)
    yield widget, controller, comm
    controller.shutdown(3000)


def _run(widget, code, ports="out"):
    widget.paramWidget.codeEditor.setText(code)
    widget.paramWidget.portsEdit.setText(ports)
    assert widget.runButton.isEnabled()
    widget.runButton.click()


def _pane(widget):
    return widget.paramWidget.outputPane.toPlainText()


def test_a_run_returns_at_once_shows_it_is_running_then_publishes(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\ntime.sleep(0.4)\nout = data * 2\n")
    assert widget.isRunning() and controller._runner.isRunning()      # the click returned while it runs
    assert not widget.runButton.isEnabled() and (not widget.cancelButton.isHidden())
    assert widget.statusLabel.text() == "Running Python step…"
    qtbot.waitUntil(lambda: len(comm.produced) == 1, timeout=5000)
    assert not widget.isRunning() and widget.cancelButton.isHidden() and widget.runButton.isEnabled()
    assert widget.statusLabel.text() == "Created rec (out)."
    assert np.array_equal(comm.produced[0][0].data, _stack().data * 2)


def test_what_the_script_prints_appears_in_the_pane_while_it_is_still_running(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\nprint('step one', flush=True)\ntime.sleep(0.5)\nprint('step two')\nout = data\n")
    qtbot.waitUntil(lambda: "step one" in _pane(widget), timeout=5000)
    assert widget.isRunning() and "step two" not in _pane(widget)
    qtbot.waitUntil(lambda: len(comm.produced) == 1, timeout=5000)
    assert _pane(widget) == "step one\nstep two"                       # the final state is the same text, not doubled


def test_cancel_stops_a_script_that_never_checks_and_publishes_nothing(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\nprint('working', flush=True)\nwhile True:\n    time.sleep(0.005)\n")
    qtbot.waitUntil(lambda: "working" in _pane(widget), timeout=5000)
    widget.cancelButton.click()
    assert widget.cancelButton.text() == "Cancelling…" and not widget.cancelButton.isEnabled()
    assert widget.statusLabel.text() == "Cancelling…"
    qtbot.waitUntil(lambda: not widget.isRunning(), timeout=8000)
    assert comm.produced == []
    assert widget.statusLabel.text() == "Cancelled: nothing was published."
    assert _pane(widget) == "working\nCancelled."                       # what it printed is kept
    assert widget.runButton.isEnabled() and widget.cancelButton.isHidden()
    assert widget.cancelButton.text() == "Cancel"


def test_a_failing_script_shows_its_line_under_what_it_printed(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "print('before')\nx = undefined_name\n")
    qtbot.waitUntil(lambda: not widget.isRunning(), timeout=5000)
    assert _pane(widget) == "before\nline 2: NameError: name 'undefined_name' is not defined"
    assert widget.statusLabel.text() == "line 2: NameError: name 'undefined_name' is not defined"
    assert comm.produced == [] and widget.runButton.isEnabled()


def test_a_second_request_while_running_is_refused_not_queued(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\ntime.sleep(0.5)\nout = data\n")
    stack = _stack()
    controller.runProcessor([stack], {"code": "out = data + 100", "ports": "out"})
    assert widget.statusLabel.text() == "Python step is already running."
    qtbot.waitUntil(lambda: not widget.isRunning(), timeout=5000)
    assert len(comm.produced) == 1
    assert np.array_equal(comm.produced[0][0].data, _stack().data)       # the first run's result, not the second's


def test_the_run_button_stays_off_while_running_however_the_inputs_change(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\ntime.sleep(0.4)\nout = data\n")
    widget.setAvailableResults([("rec", _stack()), ("other", _stack("other"))], [])   # inputs change mid-run
    assert widget.isRunning() and not widget.runButton.isEnabled()
    qtbot.waitUntil(lambda: not widget.isRunning(), timeout=5000)
    assert widget.runButton.isEnabled() == bool(widget.selectedInputs())           # decided again once it ended


def test_a_panel_without_streaming_runs_off_the_gui_thread_too(qtbot):
    from imswitch.improcess.processors.filters import FilterProcessor

    widget = ResultProcessorWidget(FilterProcessor())
    comm = _Comm()
    controller = ResultProcessorController(commChannel=comm, widget=widget, factory=None, moduleCommChannel=None)
    stack = _stack()
    widget.setCurrentResult(stack)
    qtbot.addWidget(widget)
    widget.runButton.click()
    assert widget.isRunning()
    qtbot.waitUntil(lambda: len(comm.produced) == 1, timeout=5000)
    assert not widget.isRunning() and widget.statusLabel.text().startswith("Created")
    controller.shutdown()


def test_the_bulk_publish_confirmation_still_applies_to_a_finished_run(qtbot, panel, monkeypatch):
    import imswitch.improcess.controller.ResultProcessorController as module

    widget, controller, comm = panel
    monkeypatch.setattr(module, "confirm_bulk_publish", lambda parent, results: False)
    _run(widget, "out = data\n")
    qtbot.waitUntil(lambda: not widget.isRunning(), timeout=5000)
    assert comm.produced == [] and "Cancelled: would have created 1 results." in widget.statusLabel.text()


def test_a_widget_hook_that_raises_does_not_stop_the_run_or_the_publishing(qtbot, panel):
    widget, controller, comm = panel
    widget.paramWidget.output_appended = lambda text: 1 / 0
    widget.paramWidget.before_run = lambda: 1 / 0
    _run(widget, "print('noisy')\nout = data\n")
    qtbot.waitUntil(lambda: len(comm.produced) == 1, timeout=5000)


def test_shutdown_stops_a_running_script(qtbot, panel):
    widget, controller, comm = panel
    _run(widget, "import time\nprint('up', flush=True)\nwhile True:\n    time.sleep(0.005)\n")
    qtbot.waitUntil(lambda: "up" in _pane(widget), timeout=5000)
    assert controller.shutdown(3000) is True
    assert not controller._runner.isRunning()


def test_a_cancel_with_nothing_running_does_nothing(qtbot, panel):
    widget, controller, comm = panel
    controller.cancelRun()
    assert widget.statusLabel.text() != "Cancelling…" and not widget.isRunning()

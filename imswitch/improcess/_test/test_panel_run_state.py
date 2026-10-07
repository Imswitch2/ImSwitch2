"""Run / Cancel in every processor panel: the shared state, and the three
hand-built panels (Segmentation, PSF resolution, Colocalization) running off
the GUI thread through the same controller as the generic one."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# -- the shared state -------------------------------------------------------------

def test_run_state_turns_run_off_and_cancel_on_and_remembers_what_the_inputs_said(qapp):
    from qtpy import QtWidgets

    from imswitch.improcess.view.runstate import RunState

    layout = QtWidgets.QHBoxLayout()
    before, run, after = QtWidgets.QPushButton("before"), QtWidgets.QPushButton("Run"), QtWidgets.QPushButton("after")
    for button in (before, run, after):
        layout.addWidget(button)
    cancelled = []
    state = RunState(run, layout, lambda: cancelled.append(True))
    assert layout.indexOf(state.cancelButton) == layout.indexOf(run) + 1        # right after Run
    assert state.cancelButton.isHidden() and run.isEnabled() and not state.running

    state.setRunEnabled(False)
    assert not run.isEnabled()
    state.setRunEnabled(True)
    state.setRunning(True)
    assert state.running and not run.isEnabled() and not state.cancelButton.isHidden()
    state.setRunEnabled(True)                                 # an input changes mid-run ...
    assert not run.isEnabled()                                # ... and Run stays off
    state.cancelButton.click()
    assert cancelled == [True] and state.cancelButton.text() == "Cancelling…" and not state.cancelButton.isEnabled()
    state.setRunning(False)
    assert run.isEnabled() and state.cancelButton.isHidden() and state.cancelButton.text() == "Cancel"

    state.setRunEnabled(False)                                # what the inputs said while it ran is applied now
    state.setRunning(True)
    state.setRunning(False)
    assert not run.isEnabled()


def test_run_state_starts_from_whatever_the_run_button_was(qapp):
    from qtpy import QtWidgets

    from imswitch.improcess.view.runstate import RunState

    layout = QtWidgets.QHBoxLayout()
    run = QtWidgets.QPushButton("Run")
    run.setEnabled(False)
    layout.addWidget(run)
    state = RunState(run, layout, lambda: None)
    state.setRunning(True)
    state.setRunning(False)
    assert not run.isEnabled()


# -- the hand-built panels ---------------------------------------------------------

def _image(name="img"):
    rng = np.random.default_rng(0)
    data = rng.random((32, 32)).astype(np.float32)
    data[8:16, 8:16] += 2.0
    return ArrayProcessingResult(name, data, ["Y", "X"])


def _panels(qapp):
    from imswitch.improcess.view.ColocalizationWidget import ColocalizationWidget
    from imswitch.improcess.view.PSFResolutionWidget import PSFResolutionWidget
    from imswitch.improcess.view.SegmentationWidget import SegmentationWidget

    return [
        ("segmentation", SegmentationWidget(None), "runButton"),
        ("psf-resolution", PSFResolutionWidget(napariViewer=None), "fitButton"),
        ("colocalization", ColocalizationWidget(napariViewer=None), "runButton"),
    ]


def test_each_hand_built_panel_shows_run_and_cancel_state(qapp):
    for name, widget, run_attr in _panels(qapp):
        run = getattr(widget, run_attr)
        cancels = []
        widget.sigCancelRequested.connect(lambda: cancels.append(True))
        widget.setCurrentResult(_image())
        assert widget.isRunning() is False and widget.cancelButton.isHidden(), name
        allowed = run.isEnabled()
        widget.setRunning(True)
        assert widget.isRunning() and not run.isEnabled() and not widget.cancelButton.isHidden(), name
        widget.setCurrentResult(_image("another"))                      # the result changes while it runs
        assert not run.isEnabled(), name                                # Run stays off
        widget.cancelButton.click()
        assert cancels == [True], name
        widget.setRunning(False)
        assert run.isEnabled() == allowed and widget.cancelButton.isHidden(), name


def test_the_psf_panel_runs_off_the_gui_thread_and_publishes(qapp, qtbot):
    from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController
    from imswitch.improcess.view.PSFResolutionWidget import PSFResolutionWidget

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in list(self.slots):
                slot(*args)

    comm = type("Comm", (), {})()
    comm.sigCurrentResultChanged, comm.sigResultsChanged, comm.sigResultProduced = Signal(), Signal(), Signal()
    comm.getAllResults = comm.getSelectedResults = lambda: []
    produced = []
    comm.sigResultProduced.connect(lambda result, name: produced.append((result, name)))

    panel = PSFResolutionWidget(napariViewer=None)
    controller = ResultProcessorController(commChannel=comm, widget=panel, factory=None, moduleCommChannel=None)
    blob = np.zeros((33, 33), np.float32)
    yy, xx = np.mgrid[:33, :33]
    blob += np.exp(-((yy - 16) ** 2 + (xx - 16) ** 2) / (2 * 2.0 ** 2))
    panel.setCurrentResult(ArrayProcessingResult("bead", blob, ["Y", "X"]))
    qtbot.addWidget(panel)
    panel.run()
    assert panel.isRunning() and not panel.fitButton.isEnabled() and not panel.cancelButton.isHidden()
    qtbot.waitUntil(lambda: len(produced) == 1, timeout=10000)
    assert not panel.isRunning() and panel.cancelButton.isHidden()
    # the panel now follows the new (table) result, which it does not fit; showing the bead again re-arms Fit
    assert panel.fitButton.isEnabled() == panel._acceptsResult(panel._currentResult)
    panel.setCurrentResult(ArrayProcessingResult("bead", blob, ["Y", "X"]))
    assert panel.fitButton.isEnabled()
    controller.shutdown()


def test_cancelling_a_hand_built_panel_discards_its_run(qapp, qtbot):
    from imswitch.improcess.controller import processor_runner
    from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController
    from imswitch.improcess.view.ColocalizationWidget import ColocalizationWidget
    import threading

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in list(self.slots):
                slot(*args)

    comm = type("Comm", (), {})()
    comm.sigCurrentResultChanged, comm.sigResultsChanged, comm.sigResultProduced = Signal(), Signal(), Signal()
    comm.getAllResults = comm.getSelectedResults = lambda: []
    produced = []
    comm.sigResultProduced.connect(lambda result, name: produced.append((result, name)))

    panel = ColocalizationWidget(napariViewer=None)
    qtbot.addWidget(panel)
    controller = ResultProcessorController(commChannel=comm, widget=panel, factory=None, moduleCommChannel=None)
    at_loop = threading.Event()

    def never_ends(result, params):
        at_loop.set()
        while True:
            pass

    panel.processor.apply = never_ends
    controller._runner = processor_runner.ProcessorRunner(escalateAfterMs=100, reinjectAfterMs=100)
    controller._runner.sigFinished.connect(controller._runFinished)
    controller._runner.sigOutput.connect(controller._runOutput)
    stack = ArrayProcessingResult("stack", np.zeros((2, 8, 8), np.float32), ["C", "Y", "X"])
    panel.setCurrentResult(stack)
    panel.run()
    assert at_loop.wait(5) and panel.isRunning()
    panel.cancelButton.click()
    qtbot.waitUntil(lambda: not panel.isRunning(), timeout=8000)
    assert produced == [] and "Cancelled: nothing was published." in panel.summaryLabel.text()
    assert panel.runButton.isEnabled() and panel.cancelButton.isHidden()
    controller.shutdown()

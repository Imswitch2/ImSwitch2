"""The image toolbar's processor operations run off the GUI thread when the
toolbar belongs to a real window, and inline otherwise (offscreen)."""

import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess._test.test_image_toolbar_controller import (  # noqa: E402
    _capture_dialog,
    _controller as inline_controller,
    _ReconstructionController,
    _Signal,
    _View,
)
from imswitch.improcess.controller.ImageToolbarController import ImageToolbarController  # noqa: E402
from imswitch.improcess.processors.projection.processor import ProjectionProcessor  # noqa: E402
from imswitch.improcess.view.MergeChannelsDialog import MergeChannelsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _WindowView(QtWidgets.QWidget):
    """A real window with the toolbar view's interface (the test double's, behind it)."""

    def __init__(self):
        super().__init__()
        self._inner = _View()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _windowed(data):
    comm = SimpleNamespace(sigCurrentResultChanged=_Signal(), sigResultProduced=_Signal())
    view = _WindowView()
    recon = _ReconstructionController(data)
    controller = ImageToolbarController(comm, view, recon)
    return controller, view, recon


def _volume():
    return np.arange(24, dtype=np.float32).reshape(2, 3, 4)


def test_a_runner_exists_only_when_the_toolbar_belongs_to_a_window(qapp):
    windowed, _view, _recon = _windowed(_volume())
    assert windowed._runner is not None
    inline, _view2, _recon2 = inline_controller(_volume())
    assert inline._runner is None
    assert windowed.shutdown() is True and inline.shutdown() is True


def test_a_projection_returns_at_once_and_publishes_when_it_is_done(qapp, qtbot):
    controller, view, _recon = _windowed(_volume())
    controller.maxProjection()
    produced = controller._commChannel.sigResultProduced.emitted
    assert produced == [] and controller._runner.isRunning()             # the click returned first
    assert view._inner.messages[-1].startswith("Running ")
    qtbot.waitUntil(lambda: len(produced) == 1, timeout=5000)
    result = produced[0][0]
    assert result.axis_labels == ["Y", "X"] and result.name == "source (max Z-projection)"
    np.testing.assert_array_equal(result.data, _volume().max(axis=0))
    assert not controller._runner.isRunning()
    controller.shutdown()


def test_the_same_operation_gives_the_same_result_inline_and_windowed(qapp, qtbot):
    inline, _v, _r = inline_controller(_volume())
    inline.splitStack()
    windowed, _v2, _r2 = _windowed(_volume())
    windowed.splitStack()
    qtbot.waitUntil(lambda: len(windowed._commChannel.sigResultProduced.emitted) == 2, timeout=5000)
    left = [args[0] for args in inline._commChannel.sigResultProduced.emitted]
    right = [args[0] for args in windowed._commChannel.sigResultProduced.emitted]
    assert [r.name for r in left] == [r.name for r in right]
    for a, b in zip(left, right):
        np.testing.assert_array_equal(a.data, b.data)
    windowed.shutdown()


def test_a_second_operation_is_refused_while_one_runs(qapp, qtbot):
    controller, view, recon = _windowed(_volume())
    release = threading.Event()

    class Slow(ProjectionProcessor):
        def apply(self, result, params):
            release.wait(5)
            return super().apply(result, params)

    controller._runProcessor(Slow(), recon.result, {"axis": "Auto", "mode": "max"})
    controller.maxProjection()
    assert view._inner.messages[-1] == "An image operation is still running."
    release.set()
    produced = controller._commChannel.sigResultProduced.emitted
    qtbot.waitUntil(lambda: len(produced) == 1, timeout=5000)
    assert len(produced) == 1                                               # the refused one never ran
    controller.shutdown()


def test_a_failure_is_shown_as_it_always_was_once_the_run_ends(qapp, qtbot):
    controller, view, recon = _windowed(_volume())

    class Broken(ProjectionProcessor):
        def apply(self, result, params):
            raise RuntimeError("boom")

    controller._runProcessor(Broken(), recon.result, {})
    qtbot.waitUntil(lambda: any("Could not run" in m for m in view._inner.messages), timeout=5000)
    assert view._inner.messages[-1] == f"Could not run {Broken().name}: boom"
    assert controller._commChannel.sigResultProduced.emitted == []
    assert not controller._runner.isRunning()
    controller.shutdown()


def test_a_dialog_driven_merge_runs_off_the_gui_thread_with_its_results_as_inputs(qapp, qtbot, monkeypatch):
    data = np.arange(4, dtype=np.float32).reshape(2, 2)
    controller, view, recon = _windowed(data)
    second = type(recon.result)(data + 10)
    second.name = "second"
    recon.all_results = [recon.result, second]
    recon.selected_results = [recon.result]
    controller.currentResultChanged(recon.result)
    _capture_dialog(monkeypatch, MergeChannelsDialog, {
        "results": [second, recon.result], "name": "Merged channels", "axis_label": "C", "composite": False,
    })
    controller.mergeChannels()
    produced = controller._commChannel.sigResultProduced.emitted
    assert produced == [] and controller._runner.isRunning()
    qtbot.waitUntil(lambda: len(produced) == 1, timeout=5000)
    merged = produced[0][0]
    assert merged.name == "Merged channels" and merged.axis_labels == ["C", "Y", "X"]
    np.testing.assert_array_equal(merged.data[0], second.data)
    np.testing.assert_array_equal(merged.data[1], recon.result.data)
    controller.shutdown()


def test_an_incompatible_merge_reports_why_after_the_run(qapp, qtbot, monkeypatch):
    controller, view, recon = _windowed(np.zeros((2, 2), np.float32))
    other = type(recon.result)(np.zeros((3, 3), np.float32))
    other.name = "other"
    recon.all_results = [recon.result, other]
    controller.currentResultChanged(recon.result)
    _capture_dialog(monkeypatch, MergeChannelsDialog, {
        "results": [recon.result, other], "name": "m", "axis_label": "C", "composite": False,
    })
    controller.mergeChannels()
    qtbot.waitUntil(lambda: any("does not match" in m for m in view._inner.messages), timeout=5000)
    assert view._inner.messages[-1].startswith("Could not merge channels: ")
    controller.shutdown()


def test_shutdown_stops_a_toolbar_operation_that_is_still_running(qapp, qtbot):
    controller, view, recon = _windowed(_volume())
    at_loop = threading.Event()

    class Spin(ProjectionProcessor):
        def apply(self, result, params):
            at_loop.set()
            while True:
                pass

    controller._runProcessor(Spin(), recon.result, {})
    assert at_loop.wait(5)
    assert controller.shutdown(3000) is True
    assert not controller._runner.isRunning()

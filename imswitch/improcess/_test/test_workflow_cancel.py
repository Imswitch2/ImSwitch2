"""Cancelling a workflow run inside a step, not only between steps: a processor
step is asked, then interrupted; a save or a reconstruction is left to finish."""

import os
import time
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.controller import WorkflowController as module  # noqa: E402
from imswitch.improcess.controller.WorkflowController import WorkflowController, _RunWorker  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
)

SPIN = "import time\nprint('in the step', flush=True)\nwhile True:\n    time.sleep(0.005)\n"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _recording(path, count=6, size=8):
    planes = np.broadcast_to(np.arange(count, dtype=np.float32)[:, None, None], (count, size, size))
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=np.array(planes))
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        dataset.attrs["axes"] = "CYX"
    return path


def _workflow(raw, code, *, save=False):
    steps = [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "python", {"code": code, "ports": "out"}, inputs=["rec"]),
    ]
    if save:
        steps.append(Save("out", input="split", fmt="hdf5"))
    return Workflow("w", steps)


def _controller(statuses=None):
    sink = statuses if statuses is not None else []
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: None), getAllResults=lambda: [])
    view = SimpleNamespace(showStatusMessage=lambda message, timeout_ms=6000: sink.append(message))
    return WorkflowController(
        comm, view, SimpleNamespace(getActiveResult=lambda: None),
        registry_factory=lambda: bootstrap_registry(user_plugins=False),
    )


# -- the Qt-free runner ----------------------------------------------------------

def test_a_cancellation_raised_inside_a_step_is_a_cancelled_run_error_with_its_report(registry, tmp_path):
    code = "from imswitch.imcommon.model import OperationCancelled\nraise OperationCancelled()\n"
    with pytest.raises(RunError) as caught:
        run(_workflow(_recording(tmp_path / "scan.h5"), code), registry=registry, out_dir=tmp_path)
    assert str(caught.value) == "split: cancelled"
    report = caught.value.report
    assert report.failed_step == "split" and report.steps_run == ["raw", "rec"]
    report.close()


def test_a_step_that_checks_the_token_stops_when_the_run_is_told_to(registry, tmp_path):
    import threading

    from imswitch.imcommon.model import CancelToken, clearCurrentCancelToken, setCurrentCancelToken

    token = CancelToken()
    code = "from imswitch.imcommon.model import checkpoint\nimport time\nwhile True:\n    checkpoint()\n    time.sleep(0.005)\n"
    outcome = {}

    def go():
        setCurrentCancelToken(token)
        try:
            run(_workflow(_recording(tmp_path / "scan.h5"), code), registry=registry, out_dir=tmp_path)
        except RunError as exc:
            outcome["error"] = exc
        finally:
            clearCurrentCancelToken()

    thread = threading.Thread(target=go)
    thread.start()
    time.sleep(0.3)
    token.requestStop()
    thread.join(5)
    assert not thread.is_alive() and str(outcome["error"]) == "split: cancelled"
    outcome["error"].report.close()


# -- which steps the worker will interrupt ---------------------------------------

def test_only_a_running_processor_step_is_interruptible(registry, tmp_path):
    workflow = _workflow(_recording(tmp_path / "scan.h5"), "out = data", save=True)
    worker = _RunWorker(workflow, registry, tmp_path)
    worker._ident = 12345
    assert worker.interrupt() is False                       # before any step
    for step_id, interruptible in (("raw", False), ("rec", False), ("split", True), ("out", False), ("done", False)):
        worker._progress(0, 4, step_id)
        assert worker._interruptible is interruptible, step_id
    worker._progress(0, 4, "split")
    worker._ident = None
    assert worker.interrupt() is False                       # not running on any thread


def test_cancel_asks_politely_first_through_the_token(registry, tmp_path):
    worker = _RunWorker(_workflow(_recording(tmp_path / "scan.h5"), "out = data"), registry, tmp_path)
    assert not worker._token.isStopRequested()
    worker.cancel()
    assert worker._cancelled and worker._token.isStopRequested()


# -- real threads ------------------------------------------------------------------

def _wait_in_the_step(qtbot, controller):
    qtbot.waitUntil(lambda: controller._worker is not None and controller._worker._interruptible, timeout=8000)
    qtbot.wait(150)                                           # let the script reach its loop


def test_cancel_run_interrupts_a_runaway_python_step(qtbot, registry, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_ESCALATE_AFTER_S", 0.2)
    statuses = []
    controller = _controller(statuses)
    path = _workflow(_recording(tmp_path / "scan.h5"), SPIN).save(tmp_path / "w.yaml")
    assert controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=True) is True
    _wait_in_the_step(qtbot, controller)
    controller.requestCancel()
    qtbot.waitUntil(lambda: controller._thread is None, timeout=10000)
    assert any("split: cancelled" in message for message in statuses)


def test_the_blocking_cancel_used_at_shutdown_also_interrupts_it(qtbot, registry, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_ESCALATE_AFTER_S", 0.2)
    controller = _controller()
    path = _workflow(_recording(tmp_path / "scan.h5"), SPIN).save(tmp_path / "w.yaml")
    assert controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=True) is True
    _wait_in_the_step(qtbot, controller)
    started = time.monotonic()
    assert controller.cancelRun(8000) is True                 # it was stopped, not orphaned
    assert time.monotonic() - started < 6
    qtbot.waitUntil(lambda: controller._thread is None, timeout=5000)


def test_shutdown_stops_a_running_workflow_with_a_runaway_step(qtbot, registry, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_ESCALATE_AFTER_S", 0.2)
    controller = _controller()
    path = _workflow(_recording(tmp_path / "scan.h5"), SPIN).save(tmp_path / "w.yaml")
    controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=True)
    _wait_in_the_step(qtbot, controller)
    assert controller.shutdown(8000) is True
    qtbot.waitUntil(lambda: controller._thread is None, timeout=5000)


def test_a_run_that_is_not_in_a_processor_step_is_not_interrupted_by_the_watchdog(qtbot, registry, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_ESCALATE_AFTER_S", 0.0)
    controller = _controller()
    interrupts = []
    path = _workflow(_recording(tmp_path / "scan.h5"), "out = data").save(tmp_path / "w.yaml")
    controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=True)
    worker = controller._worker
    original = worker.interrupt
    worker.interrupt = lambda: (interrupts.append(original()), interrupts[-1])[1]
    controller.requestCancel()
    qtbot.waitUntil(lambda: controller._thread is None, timeout=8000)
    assert all(result is False for result in interrupts)

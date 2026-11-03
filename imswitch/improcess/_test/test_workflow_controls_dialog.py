"""The run dialog for a workflow file's controls (schema 2, plan section 9):
its values reach the run; a control with no active target in that run is
shown disabled with the reason, in each of the three run commands; Cancel
runs nothing; a file without controls, or a run from the editor, opens no
dialog."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import output_node  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Control,
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
)


sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _View(QtWidgets.QWidget):
    """A main view to ask in (a real widget), that records the status line."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def showStatusMessage(self, message, timeout_ms=6000):
        self.log.append(message)


def _h5(path):
    return str(write_synthetic_recording(path, frames=2, axes="CYX"))


def _chain(source=None, *, controls=(), bypass=()):
    steps = [
        Source("raw", path=source),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("proj", "projection", {"axis": "C", "mode": "max"}, inputs=["rec"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["proj"]),
        Save("save", input="blur", fmt="tiff"),
    ]
    for step in steps:
        if step.id in bypass:
            step.bypass = True
    return Workflow("chain", steps, controls=list(controls))


def _controller(tmp_path, selected=()):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    produced, log = [], []
    comm = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda r, n: produced.append((n, r))),
        getAllResults=lambda: [("r", r) for r in selected],
        getSelectedResults=lambda: [("r", r) for r in selected],
    )
    view = _View(log)
    controller = WorkflowController(comm, view, SimpleNamespace(getActiveResult=lambda: None),
                                    registry_factory=lambda: bootstrap_registry(user_plugins=False))
    asked = []
    controller._execDialog = lambda dialog: asked.append(dialog) or QtWidgets.QDialog.Rejected
    return SimpleNamespace(controller=controller, produced=produced, log=log, asked=asked, view=view)


def _answer(rig, values=None, accept=True):
    def answer(dialog):
        rig.asked.append(dialog)
        for name, value in (values or {}).items():
            dialog.form.set_value(name, value)
        if not accept:
            return QtWidgets.QDialog.Rejected
        dialog.accept()
        return dialog.result()
    rig.controller._execDialog = answer


def _wait(rig, timeout=60.0):
    app = QtWidgets.QApplication.instance()
    deadline = time.time() + timeout
    while rig.controller._thread is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    for _ in range(5):
        app.processEvents()
    assert rig.controller._thread is None


def test_the_dialogs_values_reach_the_run(tmp_path):
    rig = _controller(tmp_path)
    path = _chain(_h5(tmp_path / "cells.h5"), controls=[Control("radius", ["blur.radius"], label="Blur")]).save(
        tmp_path / "chain.yaml")
    _answer(rig, {"radius": 2.5})
    assert rig.controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=False)
    dialog = rig.asked[0]
    assert dialog.windowTitle() == "Run chain" and dialog.values() == {"radius": 2.5}
    assert any("Running workflow chain with radius = 2.5" in m for m in rig.log)
    _wait(rig)
    blurred = next(r for n, r in rig.produced if output_node(r).get("step") == "blur" or "blur" in str(n))
    assert output_node(blurred)["params"]["radius"] == 2.5
    assert any(m.startswith("Workflow chain done") for m in rig.log)


def test_a_value_the_run_cannot_use_keeps_the_dialog_open(tmp_path):
    from imswitch.improcess.view.WorkflowControlsDialog import WorkflowControlsDialog

    dialog = WorkflowControlsDialog("chain", [{"name": "radius", "type": "float", "value": 1.0, "active": True,
                                               "targets": [{"target": "blur.radius", "active": True}]}],
                                    validate=lambda values: "control 'radius': 9.0 is above 5")
    dialog.form.set_value("radius", 9.0)
    dialog.accept()
    assert dialog.result() != QtWidgets.QDialog.Accepted and "above 5" in dialog.problem.text()


def test_a_control_on_a_bypassed_step_is_shown_disabled_with_why(tmp_path):
    rig = _controller(tmp_path)
    path = _chain(_h5(tmp_path / "cells.h5"), bypass=("blur",),
                  controls=[Control("method", ["blur.method"]), Control("mode", ["proj.mode"])]).save(
        tmp_path / "chain.yaml")
    _answer(rig, {})
    assert rig.controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=False)
    dialog = rig.asked[0]
    label = dialog.inactive_labels["method"]
    assert not label.isEnabled() and "blur is bypassed" in label.text()
    assert set(dialog.values()) == {"mode"}                       # the disabled one sends nothing
    _wait(rig)
    assert any("bypassed: blur" in m for m in rig.log)


def test_on_selected_results_a_control_on_the_replaced_reconstruction_is_disabled(tmp_path):
    stack = ArrayProcessingResult("cell", np.random.default_rng(1).random((3, 8, 8)).astype(np.float32),
                                  ["C", "Y", "X"])
    rig = _controller(tmp_path, selected=[stack])
    workflow = _chain(controls=[Control("detector", ["rec.detector"]), Control("radius", ["blur.radius"])])
    workflow.steps[1] = Reconstruct("rec", "time-lapse", inputs=["raw"])
    path = workflow.save(tmp_path / "chain.yaml")
    _answer(rig, accept=False)
    assert rig.controller.runWorkflowOnResults(str(path), out_dir=str(tmp_path / "out"), overwrite=False) is False
    dialog = rig.asked[0]
    assert "rec is replaced by the selected result" in dialog.inactive_labels["detector"].text()
    assert set(dialog.values()) == {"radius"}
    assert rig.controller._thread is None and any("Not run: chain was cancelled" in m for m in rig.log)


def test_over_files_every_control_is_offered(tmp_path):
    rig = _controller(tmp_path)
    files = [_h5(tmp_path / "a.h5"), _h5(tmp_path / "b.h5")]
    path = _chain(controls=[Control("radius", ["blur.radius"])]).save(tmp_path / "chain.yaml")
    _answer(rig, {"radius": 1.5})
    assert rig.controller.runWorkflowOverFiles(str(path), files=files, out_dir=str(tmp_path / "out"),
                                               overwrite=False)
    dialog = rig.asked[0]
    assert dialog.inactive_labels == {} and "each run" in dialog.findChildren(QtWidgets.QLabel)[0].text()
    assert rig.controller._worker.controls == {"radius": 1.5}
    _wait(rig)


def test_no_dialog_without_controls_or_from_the_editor(tmp_path):
    rig = _controller(tmp_path)
    path = _chain(_h5(tmp_path / "cells.h5")).save(tmp_path / "plain.yaml")
    assert rig.controller.runWorkflow(str(path), out_dir=str(tmp_path / "out"), overwrite=False)
    _wait(rig)
    assert rig.asked == []
    # The editor hands over the workflow itself (no file): its values are in its steps.
    controlled = _chain(_h5(tmp_path / "cells2.h5"), controls=[Control("radius", ["blur.radius"])])
    assert rig.controller.runWorkflowObject(controlled, out_dir=str(tmp_path / "out2"), overwrite=False)
    _wait(rig)
    assert rig.asked == []

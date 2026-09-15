"""Applying a workflow to results already in the GUI, and to many files.

A binding may hand the runner an in-memory result for a reconstruct step:
that step is not run, its source is not opened, and every later step runs
on the result as it is. The GUI's *Run workflow on selected results…* binds
the workflow's one reconstruction step to each selected result in turn;
*Run workflow over files…* binds its one source to each chosen file.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    WorkflowError,
    bootstrap_registry,
    run,
)
from imswitch.improcess.workflows.runner import steps_replaced_by_bindings  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
    return path


def _stack(name):
    return ArrayProcessingResult(name, np.random.default_rng(1).random((3, 8, 8)).astype(np.float32),
                                 ["Z", "Y", "X"], metadata={"origin": name})


def _chain():
    """Source -> reconstruct -> projection -> filter -> save, the exported shape."""
    return Workflow("chain", [
        Source("raw"),                                   # unbound: must never be opened
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("proj", "projection", {"axis": "Z", "mode": "max"}, inputs=["rec"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["proj"]),
        Save("save", input="blur", fmt="tiff"),
    ])


# -- the runner ---------------------------------------------------------------------------

def test_a_result_bound_to_the_reconstruct_step_skips_its_source_and_chains_provenance(registry, tmp_path):
    stack = _stack("cell 3")
    with run(_chain(), registry=registry, bindings={"rec": stack}, out_dir=tmp_path) as report:
        assert report.bound == ["rec.out"] and report.result("rec") is stack
        assert report._sources == []                       # the unbound source was never opened
        blurred = report.result("blur")
        assert blurred.data.shape == (8, 8)
        # The processing steps hang off the result's own provenance node:
        # the bound stack (which had none) got an origin node, and the
        # projection's input is exactly that node.
        origin_id = graph_of(stack)["output"]["node"]
        assert output_node(report.result("proj"))["inputs"][0]["node"] == origin_id
        assert origin_id in graph_of(blurred)["nodes"]
        assert report.receipts and report.receipts[0].primary.name == "cell-3_save.ome.tif"


def test_only_reconstruct_consolidate_and_process_steps_take_a_result(registry, tmp_path):
    with pytest.raises(WorkflowError, match="only a reconstruct"):
        run(_chain(), registry=registry, bindings={"raw": _stack("x")}, out_dir=tmp_path)
    with pytest.raises(WorkflowError, match="only a reconstruct"):
        run(_chain(), registry=registry, bindings={"save": _stack("x")}, out_dir=tmp_path)
    with pytest.raises(WorkflowError, match="does not exist"):
        run(_chain(), registry=registry, bindings={"nope": _stack("x")}, out_dir=tmp_path)


def test_steps_are_skipped_only_when_every_consumer_is_skipped():
    wf = Workflow("two", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Reconstruct("rec2", "view-only", inputs=["raw"]),
        Process("p", "projection", {"axis": "Z"}, inputs=["rec"]),
    ])
    assert steps_replaced_by_bindings(wf, {"rec"}) == {"rec"}          # raw still feeds rec2
    assert steps_replaced_by_bindings(wf, {"rec", "rec2"}) == {"rec", "rec2", "raw"}


# -- the GUI ------------------------------------------------------------------------------

def _controller(selected, produced):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    comm = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda r, n: produced.append((r, n))),
        getAllResults=lambda: [("r", r) for r in selected],
        getSelectedResults=lambda: [("r", r) for r in selected],
    )
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: produced.append(("status", m)))
    return WorkflowController(
        comm, view, SimpleNamespace(getActiveResult=lambda: None),
        registry_factory=lambda: bootstrap_registry(user_plugins=False),   # not the user's drop-in folder
    )


def test_run_on_selected_results_binds_each_result_and_publishes_only_new_ones(registry, tmp_path, monkeypatch):
    from imswitch.improcess.controller import WorkflowController as module

    selected = [_stack("a"), _stack("b")]
    produced = []
    controller = _controller(selected, produced)
    wf_path = _chain().save(tmp_path / "chain.yaml")
    captured = {}

    class SyncWorker(module._RunWorker):
        def __init__(self, workflow, registry, out_dir, parent=None, *, overwrite=False, bindings_list=None):
            captured["bindings"] = list(bindings_list)
            super().__init__(workflow, registry, out_dir, parent, overwrite=overwrite, bindings_list=bindings_list)

    monkeypatch.setattr(module, "_RunWorker", SyncWorker)
    assert controller.runWorkflowOnResults(str(wf_path), out_dir=str(tmp_path / "out"), overwrite=False) is True
    assert captured["bindings"] == [{"rec": selected[0]}, {"rec": selected[1]}]
    assert controller.cancelRun(5000) is True
    # Drive the worker synchronously to see what gets published.
    worker = SyncWorker(_chain(), registry, tmp_path / "out2", bindings_list=captured["bindings"])
    worker.sigFinished.connect(controller._onFinished)
    worker.sigFailed.connect(controller._onFailed)
    worker.run()
    published = [r for r, n in produced if r != "status"]
    assert len(published) == 4                                   # proj + blur per input, never the inputs
    assert not any(r is s for r in published for s in selected)
    assert sorted(p.name for p in (tmp_path / "out2").iterdir()) == ["a_save.ome.tif", "b_save.ome.tif"]


def test_run_on_results_needs_exactly_one_reconstruction_step(tmp_path):
    produced = []
    controller = _controller([_stack("a")], produced)
    no_recon = Workflow("p", [Source("raw"), Save("s", input="raw", fmt="tiff")]).save(tmp_path / "n.yaml")
    assert controller.runWorkflowOnResults(str(no_recon), out_dir=str(tmp_path)) is False
    assert any("no reconstruction step" in m for k, m in produced if k == "status")
    two = Workflow("t", [Source("raw"), Reconstruct("r1", "view-only", inputs=["raw"]),
                         Reconstruct("r2", "view-only", inputs=["raw"])]).save(tmp_path / "t.yaml")
    assert controller.runWorkflowOnResults(str(two), out_dir=str(tmp_path)) is False
    assert any("2 reconstruction steps" in m for k, m in produced if k == "status")
    controller = _controller([], produced)
    assert controller.runWorkflowOnResults(str(two), out_dir=str(tmp_path)) is False
    assert any("Select one or more results" in m for k, m in produced if k == "status")


def test_run_over_files_binds_the_single_source_to_each_file(tmp_path, monkeypatch):
    from imswitch.improcess.controller import WorkflowController as module

    produced = []
    controller = _controller([], produced)
    files = [_h5(tmp_path / "one.h5"), _h5(tmp_path / "two.h5")]
    wf_path = _chain().save(tmp_path / "chain.yaml")
    captured = {}

    class Capture(module._RunWorker):
        def __init__(self, workflow, registry, out_dir, parent=None, *, overwrite=False, bindings_list=None):
            captured["bindings"] = list(bindings_list)
            super().__init__(workflow, registry, out_dir, parent, overwrite=overwrite, bindings_list=bindings_list)

    monkeypatch.setattr(module, "_RunWorker", Capture)
    assert controller.runWorkflowOverFiles(str(wf_path), files=files, out_dir=str(tmp_path / "o"), overwrite=False)
    assert [b["raw"].path for b in captured["bindings"]] == [str(f) for f in files]
    assert controller.cancelRun(5000) is True


def test_a_failing_row_does_not_stop_the_batch(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import _RunWorker

    good = _stack("good")
    bad = ArrayProcessingResult("bad", np.zeros((8, 8), np.float32), ["Y", "X"])   # projection over Z fails
    worker = _RunWorker(_chain(), registry, tmp_path, bindings_list=[{"rec": bad}, {"rec": good}])
    outcomes = []
    worker.sigFinished.connect(lambda report: outcomes.append("ok"))
    worker.sigFailed.connect(lambda message, report: outcomes.append(f"fail:{message[:20]}"))
    worker.sigDone.connect(lambda finished, failed: outcomes.append((finished, failed)))
    worker.run()
    assert outcomes[0].startswith("fail") and outcomes[1] == "ok" and outcomes[2] == (1, 1)


def test_a_result_a_step_refuses_is_reported_and_the_rest_still_run(registry, tmp_path):
    """A localization-only step over an image: refused before the processor
    runs, reported for that row, and the next row is untouched."""
    from imswitch.improcess.controller.WorkflowController import _RunWorker
    from imswitch.improcess.model.localization_result import LocalizationResult
    from imswitch.improcess.model.localization_schema import localizations_from_columns

    wf = Workflow("smlm", [
        Source("raw"),
        Reconstruct("rec", "smlm-localizer", inputs=["raw"]),
        Process("keep", "smlm-filter", inputs=["rec"]),
    ])
    image = _stack("an image, not localizations")
    locs = LocalizationResult("locs", localizations_from_columns({
        "frame": np.array([0, 1]), "x_nm": np.array([10.0, 20.0]), "y_nm": np.array([5.0, 8.0]),
        "photons": np.array([500.0, 800.0])}), pixel_size_nm=100.0)
    worker = _RunWorker(wf, registry, tmp_path, bindings_list=[{"rec": image}, {"rec": locs}])
    outcomes = []
    worker.sigFinished.connect(lambda report: outcomes.append(("ok", report.result("keep").kind)))
    worker.sigFailed.connect(lambda message, report: outcomes.append(("fail", message, report.failed_step)))
    worker.sigDone.connect(lambda finished, failed: outcomes.append(("done", finished, failed)))
    worker.run()
    assert outcomes[0][0] == "fail" and "does not accept" in outcomes[0][1] and outcomes[0][2] == "keep"
    assert outcomes[1] == ("ok", "localization")
    assert outcomes[2] == ("done", 1, 1)


def test_the_batch_summary_names_the_failed_rows(registry, tmp_path):
    produced = []
    controller = _controller([], produced)
    bad = ArrayProcessingResult("flat", np.zeros((8, 8), np.float32), ["Y", "X"])
    from imswitch.improcess.controller.WorkflowController import _RunWorker

    worker = _RunWorker(_chain(), registry, tmp_path, bindings_list=[{"rec": bad}, {"rec": _stack("fine")}])
    worker.sigFinished.connect(controller._onFinished)
    worker.sigFailed.connect(controller._onFailed)
    worker.sigDone.connect(controller._onBatchDone)
    worker.run()
    last = [m for k, m in produced if k == "status"][-1]
    assert last.startswith("Workflow batch done: 1 run(s) succeeded, 1 failed.")
    assert "proj:" in last and "Failed:" in last


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Three things the first rig session with the branch turned up.

* the Metadata panel followed only the loaded file, so a result derived in
  the session (a copy, a crop) showed nothing new when selected;
* File -> Export workflow refused every built-in processor the setup file
  had not listed, although the GUI had loaded and run them;
* a workflow's view-only result is a lazy view over its file, which the
  viewer could not transpose.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.metadata_tree import metadata_tree_from_result  # noqa: E402
from imswitch.improcess.processors.run import run_processor  # noqa: E402
from imswitch.improcess.workflows import Reconstruct, Source, Workflow, bootstrap_registry, run  # noqa: E402


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


def _image(name="img"):
    return ArrayProcessingResult(name, np.random.default_rng(0).random((4, 4)).astype(np.float32), ["Y", "X"],
                                 metadata={"note": "hello"})


# 1. the Metadata panel follows the selected result -------------------------------------------

def test_a_result_tree_carries_identity_metadata_and_provenance(registry):
    processor = registry.get_processor("projection")
    stack = ArrayProcessingResult("stack", np.zeros((3, 4, 4), np.float32), ["Z", "Y", "X"], metadata={"tag": 1})
    results, _ = run_processor(processor, [stack], {**type(processor).default_params(), "axis": "Z", "mode": "max"}, None)
    tree = metadata_tree_from_result(results[0])
    paths = {node.path for node in tree.iter_nodes()}
    assert tree.name == results[0].name
    assert "/result/result_uid" in paths and "/result/axis_labels" in paths
    assert "/provenance/steps" in paths and "/provenance/graph" in paths
    steps = tree.find("/provenance/steps")
    assert steps is not None and any("projection" in str(item) for item in steps.value)
    plain = metadata_tree_from_result(_image())
    assert plain.find("/metadata/note").value == "hello" and plain.find("/provenance") is None


def test_the_panel_switches_to_the_selected_result_and_back_to_the_file(tmp_path):
    from imswitch.improcess._test.test_metadata_widget import _FakeDataObj, _FakeSignal, _make_controller
    from imswitch.improcess.view.MetadataWidget import MetadataWidget

    recording = _h5(tmp_path / "rec.h5")
    widget = MetadataWidget()
    comm = SimpleNamespace(sigCurrentDataChanged=_FakeSignal(), sigCurrentResultChanged=_FakeSignal())
    _make_controller(widget, comm)
    comm.sigCurrentDataChanged.emit(_FakeDataObj(recording))
    assert widget.sourceLabelText() == str(recording)

    copy = ArrayProcessingResult.duplicate(_image("recon"))
    comm.sigCurrentResultChanged.emit(copy)
    assert "recon (duplicate)" in widget.sourceLabelText() and "result in session" in widget.sourceLabelText()
    assert widget.currentTree().find("/result/result_uid").value == copy.result_uid
    assert widget.reloadButton.isEnabled()

    comm.sigCurrentResultChanged.emit(None)                    # deselecting keeps the last view
    assert "recon (duplicate)" in widget.sourceLabelText()
    comm.sigCurrentDataChanged.emit(_FakeDataObj(recording))   # a new file takes over again
    assert widget.sourceLabelText() == str(recording)


def test_a_curve_result_without_axes_is_ignored_by_the_panel(tmp_path):
    from imswitch.improcess._test.test_metadata_widget import _FakeSignal, _make_controller
    from imswitch.improcess.view.MetadataWidget import MetadataWidget

    widget = MetadataWidget()
    comm = SimpleNamespace(sigCurrentDataChanged=_FakeSignal(), sigCurrentResultChanged=_FakeSignal())
    _make_controller(widget, comm)
    comm.sigCurrentResultChanged.emit(SimpleNamespace(name="not a result"))
    assert widget.currentTree() is None


# 2. export and run see every installed plugin, whatever the setup lists ------------------------

def _workflow_controller(loaded, config):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    active = {"result": None}
    recon = SimpleNamespace(getActiveResult=lambda: active["result"])
    return WorkflowController(comm, view, recon, processing_config=config), active


def test_export_works_for_a_runtime_loaded_processor_the_setup_did_not_list(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        source_result = report.result("rec")
        processor = registry.get_processor("resize")
        results, _ = run_processor(processor, [source_result], type(processor).default_params(), None)
    controller, active = _workflow_controller([], {"reconstructors": ["view-only"], "processors": []})
    active["result"] = results[0]
    written = controller.exportWorkflow(tmp_path / "exported.yaml")
    assert written is not None
    text = Path(written).read_text()
    assert "processor: resize" in text and "reconstructor: view-only" in text


def test_run_passes_the_overwrite_choice_to_the_worker(monkeypatch, registry, tmp_path):
    from imswitch.improcess.controller import WorkflowController as module

    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    wf_path = wf.save(tmp_path / "w.yaml")
    made = {}

    class FakeWorker(module._RunWorker):
        def __init__(self, workflow, registry, out_dir, parent=None, *, overwrite=False):
            made["overwrite"] = overwrite
            super().__init__(workflow, registry, out_dir, parent, overwrite=overwrite)

    monkeypatch.setattr(module, "_RunWorker", FakeWorker)
    controller, _ = _workflow_controller([], {"processors": []})
    assert controller.runWorkflow(str(wf_path), out_dir=str(tmp_path / "out"), overwrite=True) is True
    assert made["overwrite"] is True
    assert controller.cancelRun(5000) is True


# 3. the viewer shows a lazy result -------------------------------------------------------------

def test_the_viewer_materialises_a_lazy_view_only_result(registry, tmp_path):
    from imswitch.improcess._test.test_reconstruction_viewer_sot import _bareController

    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        result = report.result("rec")
        assert not hasattr(result.data, "transpose")          # the lazy view the rig saw
        controller = _bareController()
        controller._setProcessingResultSlice(result)
        assert controller._widget.set_image_calls == 1
        assert controller._displayedAxisLabels == list(result.axis_labels)


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

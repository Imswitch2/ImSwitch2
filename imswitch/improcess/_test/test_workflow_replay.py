"""A saved file replays into the pipeline that made it, or says why not."""

import os

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node, record_import  # noqa: E402
from imswitch.improcess.model.provenance_io import read_provenance  # noqa: E402
from imswitch.improcess.model.save_protocol import ProvenanceDocument  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    ReplayError,
    RunError,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    workflow_from_file,
    workflow_from_provenance,
)


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
    return path


def _strip(graph):
    """The graph minus what a fresh run legitimately changes (ids, times)."""
    stripped = []
    for node in graph["nodes"].values():
        item = {k: v for k, v in node.items() if k not in ("time", "inputs", "input_labels", "source")}
        if "source" in node:
            item["source"] = {k: v for k, v in node["source"].items() if k != "fingerprint"}
        stripped.append(item)
    return sorted(stripped, key=lambda n: (n["op"], n.get("plugin_id", ""), str(n.get("params"))))


def _diamond(raw):
    return Workflow("diamond", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("a", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["split.C0"]),
        Process("b", "filter", {"method": "median", "radius": 2.0}, inputs=["split.C1"]),
        Process("merge", "channel-merge", inputs=["a", "b"]),
        Save("out", input="merge", fmt="hdf5"),
    ])


def test_run_save_replay_reproduces_graph_and_pixels(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    with run(_diamond(raw), registry=registry, out_dir=tmp_path / "first") as report:
        original = report.result("merge")
        saved = report.receipts[0].primary

    replay = workflow_from_file(saved, registry=registry)
    wf = replay.workflow
    kinds = [type(s).__name__ for s in wf.steps]
    assert kinds == ["Source", "Reconstruct", "Process", "Process", "Process", "Process", "Save"]
    merge = wf.steps[-2]
    assert merge.processor == "channel-merge" and [str(r) for r in merge.inputs] == ["proc2.out", "proc3.out"]
    assert {str(r) for s in wf.steps[3:5] for r in s.inputs} == {"proc1.C0", "proc1.C1"}
    assert wf.steps[-1].fmt == "hdf5" and "{out_dir}" in wf.steps[-1].path_template
    assert wf.steps[0].source.path == str(raw) and wf.steps[0].source.fingerprint["shape"] == [2, 8, 8]
    assert wf.metadata["provenance_nodes"]["proc1"] in graph_of(original)["nodes"]

    with run(wf, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        replayed = again.result("proc4")
        np.testing.assert_array_equal(np.asarray(replayed.data), np.asarray(original.data))
        assert _strip(graph_of(replayed)) == _strip(graph_of(original))
        assert again.receipts[0].primary != saved                       # never the original path
        assert read_provenance(again.receipts[0].primary).graph is not None


def test_a_restricted_step_replays_with_its_geometry(registry, tmp_path):
    from imswitch.imcommon.algorithms.roi import ROIRecord
    from imswitch.improcess.analysis.roi_restriction import ROI_PARAM, ROIRestriction
    from imswitch.improcess.processors.run import run_restricted

    from types import SimpleNamespace

    from imswitch.improcess.model.provenance import record_reconstruction

    raw = _h5(tmp_path / "scan.h5")
    source = ArrayProcessingResult("src", np.ones((16, 16), np.float32), ["Y", "X"])
    record_reconstruction(
        source, registry.get_reconstructor("view-only"), {},
        SimpleNamespace(name="scan", dataPath=str(raw), datasetName="data", attrs={},
                        data_handle=np.zeros((2, 8, 8))),
    )
    processor = registry.get_processor("subtract-background")
    restriction = ROIRestriction(rois=(ROIRecord("cell", "rectangle", (2, 10, 3, 11), uid="u1"),),
                                 mode="crop", set_uid="set-1")
    restricted = run_restricted(processor, source, {"radius": 3.0}, restriction)[0]

    replay = workflow_from_provenance(ProvenanceDocument(graph=graph_of(restricted)), registry=registry)
    step = [s for s in replay.workflow.steps if isinstance(s, Process)][0]
    assert step.restriction["mode"] == "crop"
    assert step.restriction["rois"][0]["bounds"] == [2, 10, 3, 11]
    assert "summary" not in step.restriction
    assert ROI_PARAM not in step.params


def test_non_replayable_nodes_are_refused_with_every_reason(registry):
    shown = ArrayProcessingResult("src", np.ones((4, 4), np.float32), ["Y", "X"])
    imported = record_import(ArrayProcessingResult("mask", np.ones((4, 4), np.int32), ["Y", "X"]),
                             plugin_name="napari-x", widget_name="w", layer_name="l", layer_type="labels",
                             grid="fresh", source_result=shown)
    with pytest.raises(ReplayError) as excinfo:
        workflow_from_provenance(ProvenanceDocument(graph=graph_of(imported)), registry=registry)
    assert any("napari plugin" in r for r in excinfo.value.reasons)
    assert "LLM guide" in str(excinfo.value)


def test_a_missing_plugin_and_a_newer_params_version_are_refused(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"radius": 1.0}, inputs=["rec"]),
    ])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        graph = graph_of(report.result("blur"))

    node_id = output_node(report.result("blur"))
    for node in graph["nodes"].values():
        if node.get("plugin_id") == "filter":
            node["plugin_id"] = "no-such-processor"
    with pytest.raises(ReplayError, match="not installed"):
        workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry)

    for node in graph["nodes"].values():
        if node.get("plugin_id") == "no-such-processor":
            node["plugin_id"] = "filter"
            node["params_version"] = 99
    with pytest.raises(ReplayError, match="newer than the installed"):
        workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry)
    assert node_id is not None


def test_version_drift_is_a_warning_not_a_refusal(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        graph = graph_of(report.result("rec"))
    graph["imswitch_version"] = "0.0.0-old"
    for node in graph["nodes"].values():
        if node.get("op") == "reconstruct":
            node["plugin_version"] = "ancient"
    replay = workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry)
    assert any("0.0.0-old" in w for w in replay.warnings)
    assert any("ancient" in w for w in replay.warnings)


def test_replay_mode_notices_a_re_recorded_source(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Save("out", input="rec", fmt="tiff"),
    ])
    with run(wf, registry=registry, out_dir=tmp_path / "first") as report:
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    _h5(raw, shape=(3, 8, 8))
    with pytest.raises(RunError, match="differs from the recorded run"):
        run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay")
    run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay", allow_drift=True).close()


def test_a_schema_zero_history_becomes_a_best_effort_workflow(registry):
    document = ProvenanceDocument(history=[
        {"operation": "projection", "label": "Projection", "params": {"axis": "C", "mode": "max"}},
        {"operation": "filter", "label": "Filter", "params": {"radius": 2.0, "region": {"roi_set_uid": "s"}}},
    ])
    replay = workflow_from_provenance(document, registry=registry)
    kinds = [type(s).__name__ for s in replay.workflow.steps]
    assert kinds == ["Source", "Reconstruct", "Process", "Process", "Save"]
    assert replay.workflow.steps[1].reconstructor == "view-only"
    assert replay.workflow.steps[3].params == {"radius": 2.0}          # the ROI summary is not a param
    assert any("view-only" in w for w in replay.warnings)
    assert any("ROI" in w for w in replay.warnings)
    assert replay.workflow.steps[0].source.bound is False

    with pytest.raises(ReplayError, match="neither"):
        workflow_from_provenance(ProvenanceDocument(history=[{"operation": "mystery", "params": {}}]), registry=registry)


def test_a_file_with_no_provenance_is_refused(tmp_path, registry):
    import tifffile

    tifffile.imwrite(str(tmp_path / "plain.tif"), np.zeros((4, 4), np.uint8))
    with pytest.raises(ReplayError, match="no provenance"):
        workflow_from_file(tmp_path / "plain.tif", registry=registry)


def test_cli_replay_and_show_provenance(registry, tmp_path, capsys):
    from imswitch.improcess.workflows.__main__ import main

    raw = _h5(tmp_path / "scan.h5")
    with run(_diamond(raw), registry=registry, out_dir=tmp_path / "first") as report:
        saved = report.receipts[0].primary
    assert main(["--no-user-plugins", "show-provenance", str(saved)]) == 0
    shown = capsys.readouterr().out
    assert "channel-merge" in shown and "stack-split" in shown
    out_wf = tmp_path / "replayed.yaml"
    assert main(["--no-user-plugins", "replay", str(saved), "--out-workflow", str(out_wf)]) == 0
    assert out_wf.exists()
    assert main(["--no-user-plugins", "replay", str(saved), "--run", "--out", str(tmp_path / "second")]) == 0
    assert any(p.suffix == ".h5" for p in (tmp_path / "second").iterdir())


def test_gui_export_and_run_hooks(registry, tmp_path):
    """The controller's two hooks, driven without dialogs."""
    from types import SimpleNamespace

    from qtpy import QtWidgets

    from imswitch.improcess.controller.WorkflowController import WorkflowController

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"radius": 1.0}, inputs=["rec"]),
    ])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        blurred = report.result("blur")

    messages = []
    produced = []
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: messages.append(m))
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: produced.append((r, n))))
    recon = SimpleNamespace(getActiveResult=lambda: blurred)
    controller = WorkflowController(comm, view, recon)

    written = controller.exportWorkflow(tmp_path / "exported.yaml")
    assert written.exists()
    exported = Workflow.load(written)
    assert [type(s).__name__ for s in exported.steps] == ["Source", "Reconstruct", "Process", "Save"]

    recon.getActiveResult = lambda: None
    assert controller.exportWorkflow(tmp_path / "x.yaml") is None
    assert "No result" in messages[-1]

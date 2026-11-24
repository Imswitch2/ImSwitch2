"""Any image result feeds a reconstructor: chained reconstructions in a workflow.

A SNOUTY time-lapse stored with its timepoints side by side along a camera
axis had to be reshaped by a Python step before it could be deskewed, and the
deskew then refused the reshape because it was not a raw source. These tests
pin the opposite: what a reconstruction may read is decided by the data
(an image), its provenance continues the chain that made it, and a replay of
the saved result runs that chain again.
"""

import os

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.model.result_source import (  # noqa: E402
    ResultDataObj,
    data_obj_from_result,
    is_image_result,
)
from imswitch.improcess.reconstructors.run import run_reconstruction  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    validate,
    workflow_from_file,
)
from imswitch.improcess.workflows.sources import fingerprint_of  # noqa: E402

SNOUTY_PARAMS = {"device": "CPU", "c_px": 100.0, "alpha_deg": 30.0, "dy": 210.0,
                 "sample_vx_size": 200.0, "camera_offset": 100.0, "n_timepoints": 1}

#: Keeps the left half of a side-by-side camera frame: the first timepoint.
LEFT_HALF = 'out = make_result(data[..., : data.shape[-1] // 2], axes=["Z", "Y", "X"])\n'


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def side_by_side(path, *, timepoints=2, planes=8, height=16, width=16, seed=0):
    """A raw SNOUTY recording whose timepoints lie next to each other along X."""
    rng = np.random.default_rng(seed)
    volumes = [rng.random((planes, height, width)).astype(np.float32) * 100 + 101
               for _ in range(timepoints)]
    raw = np.concatenate(volumes, axis=-1)
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=raw)
        dataset.attrs["axes"] = "ZYX"
        handle.attrs["Detector:Cam:Camera pixel size"] = 0.1
    return path, volumes


def chain(raw, code=LEFT_HALF):
    return Workflow("chain", [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": code}, inputs=["view"]),
        Reconstruct("deskew", "snouty", dict(SNOUTY_PARAMS), inputs=["reshape"]),
        Save("save", input="deskew", fmt="tiff"),
    ])


# -- the bridge ----------------------------------------------------------------

def test_an_image_result_becomes_a_data_obj_without_a_file():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    result = ArrayProcessingResult("cropped", data, ["Z", "Y", "X"], axis_scales=[0.5, 0.1, 0.1],
                                   scale_unit="um")
    result.attrs = {"geometry": "skewed", "Detector:Cam:Camera pixel size": 0.1}
    data_obj = data_obj_from_result(result)
    assert isinstance(data_obj, ResultDataObj)
    assert data_obj.dataPath is None and data_obj.provenance_parent is result
    assert data_obj.axis_labels == ["Z", "Y", "X"] and data_obj.axis_scales == [0.5, 0.1, 0.1]
    assert data_obj.scale_unit == "um"
    assert data_obj.attrs["geometry"] == "skewed"
    assert data_obj.data is data                                   # no copy
    data_obj.checkAndOpenData()
    data_obj.checkAndUnloadData()
    data_obj.checkAndLoadData()
    assert data_obj.data is data                                   # unloading kept the result's array
    assert data_obj.numFrames == 2
    fingerprint = fingerprint_of(data_obj)
    assert fingerprint["shape"] == [2, 3, 4] and fingerprint["dtype"] == "float32"
    assert data_obj.acquisition_layout is not None                 # resolved from the attrs and shape


def test_a_result_named_like_a_file_is_not_that_file(tmp_path, monkeypatch):
    (tmp_path / "scan.h5").write_bytes(b"not an image")
    monkeypatch.chdir(tmp_path)
    result = ArrayProcessingResult("scan.h5", np.zeros((3, 4, 4), np.float32), ["Z", "Y", "X"])
    assert data_obj_from_result(result).dataPath is None


def test_only_image_results_can_be_bridged():
    from imswitch.improcess.model.localization_result import LocalizationResult
    from imswitch.improcess.model.localization_schema import empty_localizations

    table = LocalizationResult("locs", empty_localizations(0), pixel_size_nm=100.0)
    assert not is_image_result(table)
    with pytest.raises(ValueError, match="image data"):
        data_obj_from_result(table)


def test_the_reconstruction_provenance_continues_the_parent_chain(registry):
    parent = ArrayProcessingResult("crop", np.random.default_rng(1).random((8, 16, 16)).astype(np.float32) * 100 + 101,
                                   ["Z", "Y", "X"])
    view = registry.get_reconstructor("view-only")
    made = run_reconstruction(view, data_obj_from_result(parent), {})
    graph = graph_of(made.result)
    node = output_node(made.result)
    assert node["op"] == "reconstruct"
    parent_node = graph_of(parent)["output"]["node"]
    assert node["inputs"] == [{"node": parent_node, "port": "out"}]
    assert all(n["op"] != "source" for n in graph["nodes"].values())


# -- validation ----------------------------------------------------------------

def test_a_reconstruction_may_read_any_image_step(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    assert validate(chain(raw), registry) == []


def test_a_reconstruction_still_takes_exactly_one_input(registry):
    wf = Workflow("two", [
        Source("a", path="a.h5"), Source("b", path="b.h5"),
        Reconstruct("rec", "view-only", inputs=["a", "b"]),
    ])
    assert any("exactly one input" in issue.message for issue in validate(wf, registry))


def test_a_reconstruction_refuses_a_declared_non_image_input(registry):
    wf = Workflow("table", [
        Source("raw", path="scan.h5"),
        Reconstruct("locs", "smlm-localizer", inputs=["raw"]),
        Reconstruct("again", "view-only", inputs=["locs"]),
    ])
    issues = [issue.message for issue in validate(wf, registry)]
    assert any("must be a source or produce an image" in message and "localization" in message
               for message in issues)


# -- the chain runs, records and replays ----------------------------------------

def test_source_view_python_snouty_runs(registry, tmp_path):
    raw, volumes = side_by_side(tmp_path / "raw.h5")
    with run(chain(raw), registry=registry, out_dir=tmp_path / "out") as report:
        assert report.ok and report.steps_run == ["raw", "view", "reshape", "deskew", "save"]
        deskewed = report.result("deskew")
        assert deskewed.axis_labels == ["Z", "Y", "X"]

        # Exactly what the deskew of the first timepoint, read straight, gives.
        snouty = registry.get_reconstructor("snouty")
        alone = snouty.process(data_obj_from_result(
            ArrayProcessingResult("t0", volumes[0], ["Z", "Y", "X"])), dict(SNOUTY_PARAMS))
        np.testing.assert_allclose(np.asarray(deskewed.data), np.asarray(alone.data))

        graph = graph_of(deskewed)
        plugins = [node.get("plugin_id") for node in graph["nodes"].values()]
        assert {"view-only", "python", "snouty"} <= set(plugins)
        assert output_node(deskewed)["replayable"] is True
        python_node = next(nid for nid, n in graph["nodes"].items() if n.get("plugin_id") == "python")
        assert output_node(deskewed)["inputs"][0]["node"] == python_node


def test_the_chain_replays_from_the_saved_file(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    with run(chain(raw), registry=registry, out_dir=tmp_path / "first") as report:
        original = np.asarray(report.result("deskew").data)
        saved = report.receipts[0].primary

    replay = workflow_from_file(saved, registry=registry)
    kinds = [type(step).__name__ for step in replay.workflow.steps]
    assert kinds == ["Source", "Reconstruct", "Process", "Reconstruct", "Save"]
    deskew = replay.workflow.steps[3]
    assert deskew.reconstructor == "snouty" and str(deskew.inputs[0]) == "proc1.out"
    assert validate(replay.workflow, registry) == []
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        np.testing.assert_allclose(np.asarray(again.result("rec2").data), original)


def test_a_source_still_cannot_be_bound_to_a_result(registry, tmp_path):
    """Binding a result to a source stays refused: bind the step the result
    stands for instead (a reconstruction, a process step), or let a
    reconstruction read the result through a step that produces it."""
    from imswitch.improcess.workflows import WorkflowError

    given = ArrayProcessingResult("given", np.zeros((4, 8, 8), np.float32), ["Z", "Y", "X"])
    wf = Workflow("bound", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])])
    with pytest.raises(WorkflowError, match="cannot be bound to a result"):
        run(wf, registry=registry, bindings={"raw": given}, out_dir=tmp_path)


# -- the workflow editor ----------------------------------------------------------

def test_the_editor_connects_a_result_to_a_reconstruction():
    from imswitch.improcess.model.workfloweditor.document import WorkflowDocument
    from imswitch.improcess.model.workfloweditor.nodes import can_feed
    from imswitch.improcess.model.workfloweditor.catalog import build_catalog

    document = WorkflowDocument(Workflow("e", [
        Source("raw", path="scan.h5"),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": LEFT_HALF}, inputs=["view"]),
        Reconstruct("deskew", "snouty", inputs=["raw"]),
    ]), catalog=build_catalog())
    assert can_feed(document, "reshape", "deskew")
    assert can_feed(document, "view", "deskew")
    document.connect("reshape", "deskew")
    assert [str(ref) for ref in document.step("deskew").inputs] == ["reshape"]


# -- the GUI path ------------------------------------------------------------------

def test_the_data_list_takes_results_as_reconstruction_inputs():
    from types import SimpleNamespace

    from imswitch.improcess.controller.MultiDataFrameController import MultiDataFrameController

    rows, current = [], []

    class _Widget:
        def getAllDataObjs(self):
            return [row[2] for row in rows]

        def addDataObj(self, name, dataset, data_obj):
            rows.append((name, dataset, data_obj))

        def setDataObjMemoryFlag(self, *_):
            pass

        def setAllRowsHighlighted(self, *_):
            pass

    controller = MultiDataFrameController.__new__(MultiDataFrameController)
    controller._widget = _Widget()
    controller._logger = SimpleNamespace(warning=lambda *_: None, error=lambda *_a, **_k: None)
    controller._commChannel = SimpleNamespace(sigCurrentDataChanged=SimpleNamespace(emit=current.append))
    controller._loadingData = False
    controller.updateInfo = lambda: None
    result = ArrayProcessingResult("reshaped", np.zeros((4, 8, 8), np.float32), ["Z", "Y", "X"])
    added = controller.addResultsAsData([result, result])
    assert len(added) == 1 and len(rows) == 1                    # listed once
    assert isinstance(current[0], ResultDataObj) and current[0].provenance_parent is result

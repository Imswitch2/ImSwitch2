"""Provenance integrity for chained, auto-indexed and mapped results.

What a result made by these features must be able to answer, from its file
alone, exactly as a result made by one plain step can:

* **where it came from** -- an unbroken chain of nodes back to one source
  with its fingerprint; every input reference resolves; every non-source
  node names its plugin, plugin version and parameter version;
* **what was looped** -- one node per step (never one per index) carrying
  ``iterated_over``; ``_iterate: false`` recorded as ``iterate: false``;
* **what a map ran** -- the body as written, and each body step's own node
  (plugin, version, encoded parameters, replayable), so a body step that
  could not be replayed on its own makes the map unreplayable too;
* **that it reproduces** -- the graph survives a save (OME-TIFF and HDF5),
  passes ``validate_graph``, and a replay from the file gives the same
  pixels, bit for bit;
* **the same answer from every path** -- the runner and a direct
  ``run_reconstruction`` (the GUI's path) record the same node.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node, validate_graph  # noqa: E402
from imswitch.improcess.model.provenance_io import read_provenance  # noqa: E402
from imswitch.improcess.processors.base import Processor  # noqa: E402
from imswitch.improcess.reconstructors.run import run_reconstruction  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Map,
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

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(EXAMPLES))
from _synthetic import write_side_by_side_snouty  # noqa: E402

from imswitch.improcess._test.test_auto_indexing import RESHAPE, SNOUTY  # noqa: E402

_VOLATILE = {"time"}


class Opaque(Processor):
    """A processor whose parameters cannot be written down losslessly."""

    name = "Opaque"
    id = "test-opaque"
    kinds = ("image",)

    @classmethod
    def default_params(cls):
        return {"factor": 1.0}

    def make_param_widget(self, parent):
        return None

    def accepts(self, result):
        return True

    @property
    def applies_to(self):
        return self.accepts

    def encode_params(self, params):
        return {"factor": params.get("factor", 1.0)}, ["test: this setting is not recorded losslessly"]

    def apply(self, result, params):
        return ArrayProcessingResult(f"{result.name}*", np.asarray(result.data) * params["factor"],
                                     list(result.axis_labels))


@pytest.fixture(scope="module")
def registry():
    registry = bootstrap_registry(user_plugins=False)
    registry.register_processor(Opaque())
    return registry


@pytest.fixture
def lapse(tmp_path):
    path, volumes = write_side_by_side_snouty(tmp_path / "lapse.h5", timepoints=2)
    return path


def _chain(path):
    return Workflow.load(EXAMPLES / "snouty_side_by_side_timelapse.yaml"), {"raw": str(path)}


def _mapped(path):
    return Workflow.load(EXAMPLES / "snouty_side_by_side_timelapse_map.yaml"), {"raw": str(path)}


def _walk(graph):
    """Every node reachable from the output, each input resolving."""
    nodes = graph["nodes"]
    seen, pending = [], [graph["output"]["node"]]
    while pending:
        node_id = pending.pop()
        assert node_id in nodes, f"dangling reference to {node_id}"
        if node_id in seen:
            continue
        seen.append(node_id)
        for ref in nodes[node_id].get("inputs") or []:
            assert ref["port"] in nodes[ref["node"]]["outputs"], (node_id, ref)
            pending.append(ref["node"])
    return [nodes[node_id] for node_id in seen]


def _assert_complete(graph):
    reached = _walk(graph)
    assert set(id(n) for n in reached) == set(id(n) for n in graph["nodes"].values()), "unreachable nodes"
    sources = [n for n in reached if n["op"] == "source"]
    assert len(sources) == 1 and sources[0]["source"]["fingerprint"]["shape"]
    for node in reached:
        if node["op"] in ("reconstruct", "process"):
            assert node.get("plugin_id") and node.get("plugin_version") and node.get("params_version"), node
            assert node.get("replayable", True), node
    validate_graph(graph)
    return reached


# -- chain and loop -------------------------------------------------------------------------

def test_a_chained_auto_indexed_result_records_every_step_once(registry, lapse, tmp_path):
    workflow, bindings = _chain(lapse)
    with run(workflow, registry=registry, bindings=bindings, out_dir=tmp_path) as report:
        graph = graph_of(report.result("mona"))
        reached = _assert_complete(graph)
    ops = [(n["op"], n.get("plugin_id")) for n in reversed(reached)]
    assert ops == [("source", None), ("reconstruct", "view-only"), ("process", "python"),
                   ("reconstruct", "snouty"), ("process", "python"), ("reconstruct", "monalisa")]
    looped = {n["plugin_id"]: n.get("iterated_over") for n in reached if n["op"] == "reconstruct"}
    assert looped == {"view-only": None, "snouty": {"T": 2}, "monalisa": {"T": 2}}
    # The stacked result stands for every index: no label says "T=1/2".
    assert not any("T=1/" in label for n in reached for label in n.get("input_labels") or [])


@pytest.mark.parametrize("fmt", ["tiff", "hdf5"])
def test_the_graph_survives_the_file_and_replays_bit_for_bit(registry, lapse, tmp_path, fmt):
    workflow, bindings = _chain(lapse)
    workflow.steps[:] = [s for s in workflow.steps if not isinstance(s, Save)]
    workflow.steps.append(Save("out", input="mona", fmt=fmt))
    with run(workflow, registry=registry, bindings=bindings, out_dir=tmp_path / "a") as report:
        original = np.asarray(report.result("mona").data)
        in_memory = graph_of(report.result("mona"))
        saved = report.receipts[0].primary
    from_file = read_provenance(saved).graph
    strip = lambda g: {k: {kk: vv for kk, vv in v.items() if kk not in _VOLATILE} for k, v in g["nodes"].items()}  # noqa: E731
    assert strip(from_file) == strip(in_memory) and from_file["output"] == in_memory["output"]
    _assert_complete(from_file)
    replay = workflow_from_file(saved, registry=registry)
    assert validate(replay.workflow, registry) == [] and not replay.warnings
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "b", mode="replay") as again:
        last = [s for s in replay.workflow.steps if not isinstance(s, Save)][-1]
        np.testing.assert_array_equal(np.asarray(again.result(last.id).data), original)
        assert output_node(again.result(last.id))["iterated_over"] == {"T": 2}


def test_iterate_false_is_recorded_and_replayed(registry, tmp_path):
    # The old layout: two timepoints along the plane axis, split by SNOUTY itself.
    source = ArrayProcessingResult("lapse", np.random.default_rng(1).random((16, 16, 16)).astype(np.float32) + 101,
                                   ["Z", "Y", "X"])
    snouty = registry.get_reconstructor("snouty")
    made = run_reconstruction(snouty, source, {**SNOUTY, "n_timepoints": 2, "_iterate": False}).result
    node = output_node(made)
    assert node["iterate"] is False and "iterated_over" not in node and "_iterate" not in node["params"]


def test_the_gui_path_and_the_runner_record_the_same_node(registry, lapse, tmp_path):
    reshaped_code = RESHAPE
    workflow = Workflow("w", [
        Source("raw", path=str(lapse)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": reshaped_code}, inputs=["view"]),
        Reconstruct("deskew", "snouty", dict(SNOUTY), inputs=["reshape"]),
    ])
    with run(workflow, registry=registry, out_dir=tmp_path) as report:
        by_runner = output_node(report.result("deskew"))
        reshaped = report.result("reshape")
        snouty = registry.get_reconstructor("snouty")
        params = snouty.prepare_params(reshaped, {**type(snouty).default_params(), **SNOUTY})
        by_gui = output_node(run_reconstruction(snouty, reshaped, params).result)
    keep = ("op", "plugin_id", "plugin_version", "params_version", "params", "iterated_over", "replayable", "inputs")
    assert {k: by_runner.get(k) for k in keep} == {k: by_gui.get(k) for k in keep}


# -- map ---------------------------------------------------------------------------------------

def test_a_map_records_each_body_step_as_it_would_record_itself(registry, lapse, tmp_path):
    workflow, bindings = _mapped(lapse)
    with run(workflow, registry=registry, bindings=bindings, out_dir=tmp_path) as report:
        graph = graph_of(report.result("per_t"))
        reached = _walk(graph)
    validate_graph(graph)
    node = next(n for n in reached if n["op"] == "map")
    assert node["iterated_over"] == {"T": 2} and node["replayable"] is True
    steps = node["body_steps"]
    assert list(steps) == ["deskew", "crop", "mona"]
    assert {k: v["plugin_id"] for k, v in steps.items()} == {"deskew": "snouty", "crop": "python", "mona": "monalisa"}
    assert all(v["plugin_version"] and v["params_version"] and isinstance(v["params"], dict) for v in steps.values())
    assert steps["deskew"]["params"]["alpha_deg"] == 30.0 and "code" in steps["crop"]["params"]
    assert "body_params_by_index" not in node        # nothing differed between timepoints
    # The body's own nodes are not in the graph: one node for the whole map.
    assert not any(n.get("plugin_id") in ("snouty", "monalisa") for n in reached)


def test_a_body_step_that_cannot_be_replayed_makes_the_map_unreplayable(registry, tmp_path):
    raw, _ = write_side_by_side_snouty(tmp_path / "lapse.h5", timepoints=2, size=16)
    workflow = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": RESHAPE}, inputs=["view"]),
        Map("per_t", "reshape", "T", body=[
            {"step": "process", "id": "scale", "processor": "test-opaque", "params": {"factor": 2.0},
             "inputs": ["$item"]},
        ]),
        Save("out", input="per_t", fmt="tiff"),
    ])
    with run(workflow, registry=registry, out_dir=tmp_path / "a") as report:
        node = output_node(report.result("per_t"))
        saved = report.receipts[0].primary
    assert node["replayable"] is False
    assert any("body step 'scale'" in reason and "not recorded losslessly" in reason for reason in node["reasons"])
    # Replay refuses it and says why, as for any unreplayable step.
    from imswitch.improcess.workflows.replay import ReplayError

    with pytest.raises(ReplayError, match="body step 'scale'.*not recorded losslessly"):
        workflow_from_file(saved, registry=registry)


def test_a_mapped_result_replays_bit_for_bit_from_its_file(registry, lapse, tmp_path):
    workflow, bindings = _mapped(lapse)
    with run(workflow, registry=registry, bindings=bindings, out_dir=tmp_path / "a") as report:
        original = np.asarray(report.result("per_t").data)
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    assert validate(replay.workflow, registry) == [] and not replay.warnings
    step = next(s for s in replay.workflow.steps if isinstance(s, Map))
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "b", mode="replay") as again:
        np.testing.assert_array_equal(np.asarray(again.result(step.id).data), original)
        assert output_node(again.result(step.id))["body_steps"].keys() == {"deskew", "crop", "mona"}


def test_a_stack_keeps_only_the_attributes_every_index_agrees_on():
    from imswitch.improcess.model.core_dims import CoreMatch
    from imswitch.improcess.model.iteration import stack_results

    pieces = []
    for t in range(3):
        piece = ArrayProcessingResult("p", np.zeros((4, 4), np.float32), ["Y", "X"])
        piece.attrs = {"geometry": "deskewed", "pixel_size_nm": 77.0, "fitted_offset": float(t),
                       "grid": np.arange(3)}
        pieces.append(piece)
    stacked = stack_results(pieces, CoreMatch(("T",), (3,), ("Y", "X")))
    assert sorted(stacked.attrs) == ["geometry", "grid", "pixel_size_nm"]

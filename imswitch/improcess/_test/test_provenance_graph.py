"""The provenance graph can represent what the linear footprint could not.

A channel merge has two parents; a background subtraction has two children;
a consolidation has N. The old list copied from input zero lost all three.
These tests pin the graph on exactly those shapes, plus the two properties
replay depends on: parameters are recorded losslessly or the node says it
cannot be replayed, and a graph read from a file we did not write is checked
before anything walks it.
"""

import json

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.footprint import history_of
from imswitch.improcess.model import provenance as prov
from imswitch.improcess.processors.base import (
    Processor,
    ProcessorOutput,
    normalize_processor_output,
)


# --------------------------------------------------------------------------
# fixtures: tiny processors that exercise arity and outputs, no real algorithms
# --------------------------------------------------------------------------

def _image(name="src", shape=(2, 8, 8), labels=("C", "Y", "X")):
    return ArrayProcessingResult(
        name=name,
        data=np.random.default_rng(0).random(shape).astype(np.float32),
        axis_labels=list(labels),
    )


class _Base(Processor):
    category = "Test"

    @property
    def applies_to(self):
        return lambda result: True

    def make_param_widget(self, parent):  # pragma: no cover - never built here
        raise AssertionError("no widgets in these tests")


class _Split(_Base):
    name = "Split"
    id = "t.split"

    def apply(self, result, params):
        data = np.asarray(result.data)
        outputs = [
            ArrayProcessingResult(
                name=f"{result.name} C{index}",
                data=data[index],
                axis_labels=["Y", "X"],
            )
            for index in range(data.shape[0])
        ]
        return ProcessorOutput(outputs, keys=[f"C{index}" for index in range(len(outputs))])


class _Filter(_Base):
    name = "Filter"
    id = "t.filter"

    def apply(self, result, params):
        return ArrayProcessingResult(
            name=f"{result.name} f",
            data=np.asarray(result.data) * params.get("gain", 1.0),
            axis_labels=list(result.axis_labels),
        )


class _Merge(_Base):
    name = "Merge"
    id = "t.merge"
    min_inputs = 2
    max_inputs = None

    def apply(self, result, params):
        stack = np.stack([np.asarray(item.data) for item in params["results"]])
        return ArrayProcessingResult(name="merged", data=stack, axis_labels=["C", "Y", "X"])


def _run(processor, result, params=None, inputs=()):
    params = dict(params or {})
    if inputs:
        params["results"] = list(inputs)
        result = inputs[0]
    return normalize_processor_output(
        processor.apply(result, params), result, processor, params, inputs
    )


# --------------------------------------------------------------------------
# shapes the linear footprint could not hold
# --------------------------------------------------------------------------

def test_a_split_gives_each_output_its_own_port():
    a, b = _run(_Split(), _image())
    ga, gb = prov.graph_of(a), prov.graph_of(b)

    assert prov.output_of(ga) != prov.output_of(gb)
    assert prov.output_of(ga)[0] == prov.output_of(gb)[0]      # same step
    assert {prov.output_of(ga)[1], prov.output_of(gb)[1]} == {"C0", "C1"}
    assert prov.output_node(a)["outputs"] == ["C0", "C1"]


def test_a_diamond_keeps_both_branches_and_one_source():
    """split -> filter each channel -> merge: the classic case the list lost."""
    source = _image()
    c0, c1 = _run(_Split(), source)
    f0 = _run(_Filter(), c0, {"gain": 2.0})[0]
    f1 = _run(_Filter(), c1, {"gain": 3.0})[0]
    merged = _run(_Merge(), None, inputs=[f0, f1])[0]

    graph = prov.graph_of(merged)
    nodes = graph["nodes"]
    ops = sorted(node["op"] for node in nodes.values())
    # opaque source + split + 2 filters + merge = 5 nodes, the split once
    assert ops == ["opaque", "process", "process", "process", "process"]
    merge_node = prov.output_node(merged)
    assert merge_node["plugin_id"] == "t.merge"
    assert merge_node["inputs"] == [
        prov.output_ref(f0), prov.output_ref(f1)
    ]
    # both branches reach the same split node through different ports
    split_refs = {
        (nodes[ref["node"]]["plugin_id"], ref["port"])
        for node in nodes.values() if node.get("plugin_id") == "t.filter"
        for ref in node["inputs"]
    }
    assert split_refs == {("t.split", "C0"), ("t.split", "C1")}
    gains = sorted(
        node["params"]["gain"] for node in nodes.values() if node.get("plugin_id") == "t.filter"
    )
    assert gains == [2.0, 3.0]


def test_the_readable_history_follows_the_primary_input():
    source = _image()
    c0, c1 = _run(_Split(), source)
    f1 = _run(_Filter(), c1, {"gain": 3.0})[0]
    merged = _run(_Merge(), None, inputs=[f1, c0])[0]

    labels = [step["label"] for step in history_of(merged)]
    assert labels == ["Split", "Filter", "Merge"]
    assert history_of(merged)[-1]["inputs"][0].startswith(f"{f1.name} [")
    assert history_of(merged)[-1]["params"] == {}


def test_a_source_is_not_grown_by_its_children():
    source = _image()
    _run(_Filter(), source, {"gain": 2.0})
    _run(_Filter(), source, {"gain": 5.0})
    # the source got an opaque node so children could reference it, and no more
    assert [node["op"] for node in prov.graph_of(source)["nodes"].values()] == ["opaque"]
    assert history_of(source) == []


# --------------------------------------------------------------------------
# reconstruction and consolidation
# --------------------------------------------------------------------------

class _DataObj:
    def __init__(self, path, name="scan", dataset="data"):
        self.name = name
        self.dataPath = str(path)
        self.datasetName = dataset
        self.attrs = {"ScanTTL:Nx": 4, "note": "x"}
        self.data_handle = np.zeros((3, 4, 4), dtype=np.uint16)
        self.sourceFingerprint = None


class _Recon:
    id = "t.recon"
    name = "Test recon"
    version = "1.2"
    params_version = 3


def test_a_reconstruction_records_its_source_and_settings(tmp_path):
    raw = tmp_path / "scan.h5"
    raw.write_bytes(b"0" * 10)
    result = _image("recon")
    prov.record_reconstruction(result, _Recon(), {"psf_fwhm_nm": 250}, _DataObj(raw))

    node = prov.output_node(result)
    assert node["op"] == "reconstruct"
    assert node["plugin_id"] == "t.recon"
    assert node["plugin_version"] == "1.2"
    assert node["params_version"] == 3
    assert node["params"] == {"psf_fwhm_nm": 250}
    source = prov.graph_of(result)["nodes"][node["inputs"][0]["node"]]
    assert source["op"] == "source"
    assert source["source"]["path"] == str(raw)
    assert source["source"]["dataset"] == "data"
    fingerprint = source["source"]["fingerprint"]
    assert fingerprint["size"] == 10
    assert fingerprint["shape"] == [3, 4, 4]
    assert fingerprint["dtype"] == "uint16"
    assert len(fingerprint["attrs_digest"]) == 64
    assert [step["operation"] for step in history_of(result)] == ["t.recon"]


def test_two_reconstructions_of_one_file_share_the_source_and_consolidate(tmp_path):
    raw = tmp_path / "scan.h5"
    raw.write_bytes(b"0")
    data_obj = _DataObj(raw)
    first = prov.record_reconstruction(_image("a"), _Recon(), {"k": 1}, data_obj)
    second = prov.record_reconstruction(_image("b"), _Recon(), {"k": 2}, data_obj)
    merged = prov.record_consolidation(_image("ab"), [first, second], _Recon())

    nodes = prov.graph_of(merged)["nodes"]
    assert sorted(node["op"] for node in nodes.values()) == [
        "consolidate", "reconstruct", "reconstruct", "source"
    ]
    node = prov.output_node(merged)
    assert node["inputs"] == [prov.output_ref(first), prov.output_ref(second)]
    assert [step["operation"] for step in history_of(merged)] == ["t.recon", "t.recon"]


# --------------------------------------------------------------------------
# parameters: lossless, or honestly not replayable
# --------------------------------------------------------------------------

def test_small_arrays_and_non_finite_floats_round_trip():
    kernel = np.arange(6, dtype=np.float32).reshape(2, 3)
    encoded = prov.encode_strict({"kernel": kernel, "fill": float("nan"), "n": np.int64(3)})
    text = json.dumps(encoded)
    decoded = prov.decode_strict(json.loads(text))
    assert np.array_equal(decoded["kernel"], kernel)
    assert decoded["kernel"].dtype == np.float32
    assert np.isnan(decoded["fill"])
    assert decoded["n"] == 3


def test_a_parameter_that_cannot_be_encoded_marks_the_node_non_replayable():
    class _ROI:
        name = "cell-3"

    result = _run(_Filter(), _image(), {"gain": 2.0, "rois": [_ROI()]})[0]
    node = prov.output_node(result)

    assert node["replayable"] is False
    assert "rois" in node["reasons"][0]
    assert node["params"]["gain"] == 2.0                  # the rest is still strict
    assert node["params"]["rois"] == ["cell-3"]           # readable, not lost
    assert history_of(result)[0]["params"]["rois"] == ["cell-3"]


def test_a_big_array_is_refused_not_summarised():
    with pytest.raises(prov.NotEncodable):
        prov.encode_strict(np.zeros((100, 100)))


def test_a_long_string_and_a_long_list_survive_untruncated():
    encoded = prov.encode_strict({"expr": "a" * 5000, "items": list(range(500))})
    assert len(encoded["expr"]) == 5000
    assert len(encoded["items"]) == 500


def test_a_napari_import_is_recorded_as_non_replayable():
    shown = _run(_Filter(), _image())[0]
    imported = prov.record_import(
        _image("mask"),
        plugin_name="napari-segment-anything",
        widget_name="SAM",
        layer_name="mask",
        layer_type="labels",
        grid="fresh",
        source_result=shown,
    )
    node = prov.output_node(imported)
    assert node["op"] == "napari-import"
    assert node["replayable"] is False
    assert node["inputs"] == [prov.output_ref(shown)]
    assert history_of(imported)[-1]["label"] == "Imported from napari-segment-anything"


# --------------------------------------------------------------------------
# the ROI restriction is recorded by the runner, with its geometry
# --------------------------------------------------------------------------

def _rois():
    from imswitch.imcommon.algorithms.roi import ROIRecord

    return (
        ROIRecord("a", "rectangle", (2, 6, 3, 7), uid="ua"),
        ROIRecord("b", "rectangle", (10, 14, 11, 15), uid="ub"),
    )


class _RoiFilter(_Filter):
    id = "t.roi-filter"
    accepts_roi = True
    preserves_grid = True


def test_a_restricted_run_records_the_region_with_its_geometry():
    from imswitch.improcess.analysis.roi_restriction import ROIRestriction
    from imswitch.improcess.processors.run import run_restricted

    class _Log:
        def exception(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError(args)

    source = _image(shape=(16, 16), labels=("Y", "X"))
    restriction = ROIRestriction(rois=_rois(), mode="crop", set_uid="set-1", set_name="cells")
    result = run_restricted(_RoiFilter(), source, {"gain": 2.0}, restriction)[0]

    node = prov.output_node(result)
    assert node["replayable"] is True
    assert node["params"] == {"gain": 2.0}                 # the restriction is not a param
    recorded = node["restriction"]
    assert recorded["by_reference"] is False
    assert recorded["mode"] == "crop"
    assert recorded["set"] == {"uid": "set-1", "name": "cells", "revision": 0}
    assert [roi["bounds"] for roi in recorded["rois"]] == [[2, 6, 3, 7], [10, 14, 11, 15]]
    assert recorded["summary"]["roi_names"] == ["a", "b"]
    # the readable chain still says which region, as it always did
    assert history_of(result)[0]["params"]["region"]["roi_set_uid"] == "set-1"

    again = ROIRestriction.from_provenance(recorded)
    assert [roi.bounds for roi in again.rois] == [(2, 6, 3, 7), (10, 14, 11, 15)]
    assert again.mode == "crop" and again.set_uid == "set-1"
    assert np.isnan(again.fill)


def test_an_oversized_restriction_is_kept_by_reference(monkeypatch):
    from imswitch.improcess.analysis.roi_restriction import ROIRestriction
    from imswitch.improcess.processors.run import run_restricted

    monkeypatch.setattr(prov, "MAX_RESTRICTION_BYTES", 10)
    source = _image(shape=(16, 16), labels=("Y", "X"))
    restriction = ROIRestriction(rois=_rois(), mode="mask", set_uid="set-2")
    result = run_restricted(_RoiFilter(), source, {}, restriction)[0]

    node = prov.output_node(result)
    assert node["replayable"] is False
    assert "by reference" in node["reasons"][0]
    assert node["restriction"]["by_reference"] is True
    assert "rois" not in node["restriction"]
    assert node["restriction"]["set"]["uid"] == "set-2"     # enough to find it again


# --------------------------------------------------------------------------
# multi-output built-ins declare their ports
# --------------------------------------------------------------------------

def test_background_subtraction_names_its_two_outputs():
    from imswitch.improcess.processors.background import SubtractBackgroundProcessor

    source = _image(shape=(8, 8), labels=("Y", "X"))
    processor = SubtractBackgroundProcessor()
    outputs = normalize_processor_output(
        processor.apply(source, {"radius": 2, "output_background": True}),
        source, processor, {"radius": 2, "output_background": True},
    )
    ports = [prov.output_of(prov.graph_of(item))[1] for item in outputs]
    assert ports == ["signal", "background"]


def test_stack_and_channel_splits_name_outputs_by_axis_and_index():
    from imswitch.improcess.processors.channel_split import ChannelSplitProcessor

    source = _image(shape=(3, 8, 8), labels=("C", "Y", "X"))
    processor = ChannelSplitProcessor()
    outputs = normalize_processor_output(processor.apply(source, {}), source, processor, {})
    ports = [prov.output_of(prov.graph_of(item))[1] for item in outputs]
    assert ports == ["C0", "C1", "C2"]


def test_unnamed_multiple_outputs_still_get_distinct_ports():
    class _Twice(_Base):
        name = "Twice"
        id = "t.twice"

        def apply(self, result, params):
            return [_image("x"), _image("y")]

    a, b = _run(_Twice(), _image())
    assert {prov.output_of(prov.graph_of(a))[1], prov.output_of(prov.graph_of(b))[1]} == {"out0", "out1"}


def test_mismatched_keys_are_rejected_at_construction():
    with pytest.raises(ValueError):
        ProcessorOutput([_image("x"), _image("y")], keys=["only-one"])
    with pytest.raises(ValueError):
        ProcessorOutput([_image("x"), _image("y")], keys=["same", "same"])


# --------------------------------------------------------------------------
# graphs from files are checked before they are walked
# --------------------------------------------------------------------------

def _valid_graph():
    result = _run(_Filter(), _image(), {"gain": 2.0})[0]
    return json.loads(json.dumps(prov.graph_of(result)))


def test_a_graph_we_wrote_validates():
    assert prov.validate_graph(_valid_graph()) is not None


def test_a_cycle_is_refused():
    graph = _valid_graph()
    out_id, _ = prov.output_of(graph)
    parent = graph["nodes"][out_id]["inputs"][0]["node"]
    graph["nodes"][parent]["inputs"] = [{"node": out_id, "port": "out"}]
    with pytest.raises(prov.ProvenanceError, match="cycle"):
        prov.validate_graph(graph)


def test_a_dangling_input_or_port_is_refused():
    graph = _valid_graph()
    out_id, _ = prov.output_of(graph)
    graph["nodes"][out_id]["inputs"] = [{"node": "nope", "port": "out"}]
    with pytest.raises(prov.ProvenanceError, match="missing input"):
        prov.validate_graph(graph)

    graph = _valid_graph()
    graph["output"]["port"] = "background"
    with pytest.raises(prov.ProvenanceError, match="unknown port"):
        prov.validate_graph(graph)


def test_too_many_nodes_are_refused(monkeypatch):
    monkeypatch.setattr(prov, "MAX_NODES", 1)
    with pytest.raises(prov.ProvenanceError, match="limit"):
        prov.validate_graph(_valid_graph())


def test_an_unknown_schema_is_refused():
    graph = _valid_graph()
    graph["schema"] = 99
    with pytest.raises(prov.ProvenanceError, match="schema"):
        prov.validate_graph(graph)


def test_merging_two_definitions_of_one_step_is_a_conflict():
    graph = _valid_graph()
    out_id, _ = prov.output_of(graph)
    other = json.loads(json.dumps(graph["nodes"]))
    other[out_id]["params"]["gain"] = 99.0
    with pytest.raises(prov.ProvenanceConflict):
        prov.merge_nodes(graph["nodes"], other)


# --------------------------------------------------------------------------
# what reaches the file, for now
# --------------------------------------------------------------------------

def test_the_graph_is_not_flattened_into_the_generic_annotations(tmp_path):
    """Until the writers learn to carry the graph (Phase 2), it must not be
    squeezed through json_safe, which would truncate its nodes into summaries
    and write something that looks like provenance and is not."""
    from imswitch.improcess.model.result_io import result_annotations

    result = _run(_Filter(), _image(), {"gain": 2.0})[0]
    assert prov.PROVENANCE_KEY not in result_annotations(result)

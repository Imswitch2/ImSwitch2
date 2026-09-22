"""Regressions for the fourth review round: each test names the finding it pins."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import (  # noqa: E402
    decode_strict,
    encode_strict,
    graph_of,
    output_node,
)
from imswitch.improcess.model.provenance_io import ProvenanceReadError, read_provenance  # noqa: E402
from imswitch.improcess.model.save_protocol import companion_json_path  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Consolidate,
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    SourceSpec,
    Workflow,
    bindings_for_inputs,
    bootstrap_registry,
    run,
    run_over,
    validate,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _h5(path, shape=(2, 8, 8), name="data", seed=0):
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset(name, data=np.random.default_rng(seed).random(shape).astype(np.float32))
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
    return path


def _image(name="img"):
    return ArrayProcessingResult(name, np.random.default_rng(0).random((4, 4)).astype(np.float32), ["Y", "X"])


# 1. failed overwrite must restore the originals -------------------------------------

def test_a_failed_overwrite_restores_the_existing_files(tmp_path, monkeypatch):
    from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult

    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    result.save(tmp_path / "d.ome.tif", "tiff")
    original = (tmp_path / "d.ome.tif").read_bytes()
    original_npy = (tmp_path / "d.ome.drift.npy").read_bytes()

    def broken(plan, document):
        np.save(plan.companions[0], result.drift_xy)
        plan.primary.write_bytes(b"new")
        raise RuntimeError("late failure")

    monkeypatch.setattr(result, "write_files", broken)
    with pytest.raises(RuntimeError):
        result.save(tmp_path / "d.ome.tif", "tiff", overwrite=True)
    assert (tmp_path / "d.ome.tif").read_bytes() == original
    assert (tmp_path / "d.ome.drift.npy").read_bytes() == original_npy
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.ome.drift.npy", "d.ome.tif"]


def test_publish_refuses_a_file_that_appeared_after_preflight(tmp_path, monkeypatch):
    """The no-clobber guarantee is the link-based publish, not the preflight."""
    import imswitch.improcess.model.save_protocol as protocol

    result = _image()
    target = tmp_path / "x.ome.tif"
    real_publish = protocol._publish

    def sneak_then_publish(staged, target_path, published):
        if target_path.name == "x.ome.tif" and not target_path.exists():
            target_path.write_bytes(b"someone else's file")      # the race
        real_publish(staged, target_path, published)

    monkeypatch.setattr(protocol, "_publish", sneak_then_publish)
    with pytest.raises(FileExistsError):
        result.save(target)
    assert target.read_bytes() == b"someone else's file"


# 2. save paths stay inside out_dir -------------------------------------------------

def test_relative_out_dir_is_not_doubled_and_escapes_are_refused(registry, tmp_path, monkeypatch):
    raw = _h5(tmp_path / "scan.h5")
    monkeypatch.chdir(tmp_path)
    wf = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Save("out", input="rec", fmt="tiff"),
    ])
    with run(wf, registry=registry, out_dir="results") as report:
        assert report.receipts[0].primary == (tmp_path / "results" / "scan_out.ome.tif").resolve()
    assert not (tmp_path / "results" / "results").exists()

    for template in ("{out_dir}/../escape{ext}", "/tmp/elsewhere{ext}"):
        wf.step("out").path_template = template
        with pytest.raises(RunError, match="outside the output directory"):
            run(wf, registry=registry, out_dir=tmp_path / "r2")


# 3. strict replay checks dataset, kind, every recorded field, and the hash ----------

def test_replay_refuses_a_different_dataset_and_a_missing_fingerprint_field(registry, tmp_path):
    from imswitch.improcess.workflows.sources import fingerprint_of, open_source

    raw = tmp_path / "two.h5"
    with h5py.File(str(raw), "w") as handle:
        for name, seed in (("a", 1), ("b", 2)):
            d = handle.create_dataset(name, data=np.random.default_rng(seed).random((2, 8, 8)).astype(np.float32))
            d.attrs["element_size_um"] = [1.0, 0.1, 0.1]
    recorded = fingerprint_of(open_source(SourceSpec(path=str(raw), dataset="a")))
    wf = Workflow("r", [
        Source("raw", SourceSpec(path=str(raw), dataset="a", fingerprint=recorded)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
    ])
    run(wf, registry=registry, out_dir=tmp_path, mode="replay").close()
    with pytest.raises(RunError, match="dataset: recorded 'a', bound 'b'"):
        run(wf, registry=registry, out_dir=tmp_path, mode="replay", bindings={"raw": f"{raw}::b"})
    # a recorded field that cannot be determined now is a mismatch, not a pass
    wf.step("raw").source = SourceSpec(path=str(raw), dataset="a",
                                       fingerprint={**recorded, "manifest": "tiles-v1"})
    with pytest.raises(RunError, match="manifest: recorded"):
        run(wf, registry=registry, out_dir=tmp_path, mode="replay")


def test_verify_hash_needs_a_recorded_hash_and_catches_changed_pixels(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5", seed=1)
    wf = Workflow("h", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    with run(wf, registry=registry, out_dir=tmp_path, hash_sources=True) as report:
        graph = graph_of(report.result("rec"))
    source = next(n for n in graph["nodes"].values() if n["op"] == "source")
    assert len(source["source"]["fingerprint"]["sha256"]) == 64

    from imswitch.improcess.workflows.replay import workflow_from_provenance
    from imswitch.improcess.model.save_protocol import ProvenanceDocument

    replay = workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry).workflow
    run(replay, registry=registry, out_dir=tmp_path / "ok", mode="replay", verify_hash=True).close()
    # same shape, dtype and size, different pixels: only the hash can tell
    _h5(raw, seed=2)
    with pytest.raises(RunError, match="sha256|mtime"):
        run(replay, registry=registry, out_dir=tmp_path / "bad", mode="replay", verify_hash=True)
    no_hash = Workflow("n", [Source("raw", SourceSpec(path=str(raw), fingerprint={"shape": [2, 8, 8]})),
                             Reconstruct("rec", "view-only", inputs=["raw"])])
    with pytest.raises(RunError, match="kept no hash"):
        run(no_hash, registry=registry, out_dir=tmp_path / "c", mode="replay", verify_hash=True)


# 4. codec output must be lossless JSON --------------------------------------------

def test_a_lossy_plugin_codec_marks_the_node_non_replayable():
    from imswitch.improcess.processors.base import Processor, normalize_processor_output

    class _Lossy(Processor):
        id = "t.lossy"
        name = "Lossy"

        @property
        def applies_to(self):
            return lambda r: True

        def make_param_widget(self, parent):
            return None

        def apply(self, result, params):
            return _image("out")

        def encode_params(self, params):
            return {"handle": object(), "pair": (1, 2)}, []

    source = _image()
    out = normalize_processor_output(_Lossy().apply(source, {}), source, _Lossy(), {"handle": 1, "pair": (1, 2)})[0]
    node = output_node(out)
    assert node["replayable"] is False
    assert sorted(r.split()[-4] for r in node["reasons"] if "lossless" in r) or node["reasons"]
    assert json.loads(json.dumps(node["params"])) == node["params"]      # what was kept is JSON


@pytest.mark.parametrize("value", [
    {"__tuple__": [1, 2]},
    {"__float__": "nan"},
    {"__dict__": [[1, 2]]},
    {"__ndarray__": [1], "dtype": "int8", "shape": [1]},
    {"outer": {"__tuple__": [1, 2]}},
    {"__tuple__": (1, 2)},
])
def test_user_values_that_look_like_markers_round_trip_unchanged(value):
    """A value the encoder's own markers could be mistaken for is escaped,
    never decoded as the marker (the round-4 follow-up finding)."""
    encoded = encode_strict(value)
    assert json.loads(json.dumps(encoded)) == encoded
    assert decode_strict(encoded) == value
    assert type(decode_strict(encoded)) is dict


def test_tuples_and_non_string_keys_survive_the_strict_codec():
    value = {"shape": (3, 4), "lut": {1: "a", (0, 1): "b"}, "plain": [1, 2]}
    encoded = encode_strict(value)
    assert json.loads(json.dumps(encoded)) == encoded
    assert decode_strict(encoded) == value
    assert isinstance(decode_strict(encoded)["shape"], tuple)


# 5. fan-out ports use the input index -----------------------------------------------

def test_fan_out_ports_are_suffixed_by_input_index(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("f", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("bg", "subtract-background", {"radius": 2.0, "output_background": True},
                inputs=["split.C0", "split.C1"]),
        Process("use", "filter", inputs=["bg.background1"]),
    ])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        assert sorted(report.ports_of("bg")) == ["background", "background1", "signal", "signal1"]


# 6. GUI keeps published lazy results readable ---------------------------------------

def test_gui_run_keeps_the_sources_behind_published_results_open(registry, tmp_path):
    from qtpy import QtWidgets

    from imswitch.improcess.controller.WorkflowController import WorkflowController

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    raw = _h5(tmp_path / "scan.h5")
    produced = []
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: produced.append(r)))
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    controller = WorkflowController(comm, view, SimpleNamespace(getActiveResult=lambda: None))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    report = run(wf, registry=registry, out_dir=tmp_path)
    controller._onFinished(report)
    assert produced and np.asarray(produced[0].data).shape == (2, 8, 8)     # still readable
    assert controller._retained and report._sources == []


# 7./8. napari attribution and dock rollback ------------------------------------------

def test_dock_failure_rolls_back_the_layers_it_added(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _controller, _endpoint

    controller, viewer, view, _ = _controller(tmp_path, _image())

    def explode(plugin_name, widget_name):
        raise RuntimeError("no dock for you")

    viewer.window.add_plugin_dock_widget = explode
    assert controller.sendTo(_endpoint(controller, "napari-skimage:gaussian")) is None
    assert list(viewer.layers) == []
    assert controller.registry.sessions() == []


def test_a_mapping_is_suggested_only_for_a_layer_that_appeared_after_one_session(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import Labels, _controller
    from imswitch.improcess.model.napari_endpoints import NapariEndpoint, OutputMapping

    controller, viewer, _, _ = _controller(tmp_path, _image())
    endpoint = NapariEndpoint(id="napari-skimage:threshold", label="T", lane="dock", plugin_name="napari-skimage",
                              widget_name="Automated Threshold", kinds=("image",), verified=True,
                              output_mappings=(OutputMapping("*", "labels", "labels", preserves_grid=True),))
    older = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "older mask"}, "labels"))
    older.source = SimpleNamespace(widget=None, parent=None)
    session = controller.sendTo(endpoint)
    newer = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "new mask"}, "labels"))
    newer.source = SimpleNamespace(widget=session.widget, parent=None)   # napari: made by that dock widget
    suggestions = controller._mappingSuggestions([older, newer])
    assert id(older) not in suggestions                 # existed before the session: not its output
    assert id(newer) in suggestions
    second = controller.sendTo(endpoint)                 # a second session whose wildcard also matches
    newest = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "newest mask"}, "labels"))
    newest.source = SimpleNamespace(widget=second.widget, parent=None)
    suggestions = controller._mappingSuggestions([newer, newest])
    assert suggestions[id(newer)][0] is endpoint and suggestions[id(newer)][2] == session.result_uid
    assert id(newest) in suggestions                    # napari's record says: session 2's widget
    unrelated = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "unrelated mask"}, "labels"))
    assert id(unrelated) not in controller._mappingSuggestions([unrelated])   # newer, matching, but no record
    assert len(controller._mappingCandidates([unrelated])[id(unrelated)]) == 2  # still offered explicitly


# 9. a closed session's export is discarded ------------------------------------------

def test_a_session_closed_during_export_discards_the_late_result(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _READER, _controller

    pending = {}

    def deferred_runner(uid, job, on_done, on_failed):
        pending["finish"] = lambda: on_done(uid, job())
        return None

    controller, viewer, _, _ = _controller(tmp_path, _image())
    controller._export_runner = deferred_runner
    session = controller.sendTo(_READER)
    temp_dir = session.temp_dir
    controller.closeSession(session.uid)
    assert session.cancelled and not temp_dir.exists()
    pending["finish"]()                                   # the worker comes back late
    assert not temp_dir.exists()
    assert viewer.opened == []


# 10. failed chunks are never "complete" -----------------------------------------------

def test_a_dropped_chunk_makes_the_final_record_partial():
    from imswitch.improcess._test.test_reconstruction_provenance_paths import _stream_worker

    worker, session = _stream_worker(expected=4)
    from imswitch.improcess.reconstructors.base import Chunk

    def failing_push(chunk, start, end):
        raise RuntimeError("bad chunk")

    finals = []
    worker.sigStackFinished.connect(finals.append)
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 0, 2))
    session.push = failing_push
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 2, 4))
    worker.finalize()
    assert output_node(finals[0])["completion"]["status"] == "partial"


def test_fewer_frames_than_expected_is_partial_too():
    from imswitch.improcess._test.test_reconstruction_provenance_paths import _stream_worker
    from imswitch.improcess.reconstructors.base import Chunk

    worker, _ = _stream_worker(expected=6)
    finals = []
    worker.sigStackFinished.connect(finals.append)
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 0, 2))
    worker.finalize()
    assert output_node(finals[0])["completion"]["status"] == "partial"


# 11./12. parameter keys and consolidation params ---------------------------------------

def test_unknown_parameters_are_rejected_for_every_plugin_kind(registry):
    wf = Workflow("p", [
        Source("raw"),
        Reconstruct("rec", "view-only", {"typo": 1}, inputs=["raw"]),
        Process("merge", "channel-merge", {"nope": 2}, inputs=["rec", "rec"]),
        Reconstruct("rec2", "monalisa", {"scan_params": {}}, inputs=["raw"]),     # allowed extra key
    ])
    messages = [str(i) for i in validate(wf, registry)]
    assert any("rec: unknown parameter(s) for 'view-only': ['typo']" in m for m in messages)
    assert any("merge: unknown parameter(s) for 'channel-merge': ['nope']" in m for m in messages)
    assert not any("rec2:" in m for m in messages)


def test_consolidation_takes_no_parameters(registry):
    from imswitch.improcess.reconstructors.run import run_consolidation

    wf = Workflow("c", [
        Source("a"), Reconstruct("ra", "monalisa", inputs=["a"]),
        Consolidate("all", "monalisa", inputs=["ra"], params={"x": 1}),
    ])
    assert any("takes no parameters" in str(i) for i in validate(wf, registry))
    with pytest.raises(ValueError, match="takes no parameters"):
        run_consolidation(registry.get_reconstructor("monalisa"), [], {"x": 1})


# 13. companion names keep the stem ---------------------------------------------------

def test_dotted_csv_names_get_distinct_companions():
    assert companion_json_path(Path("sample.v1.csv")).name == "sample.v1.provenance.json"
    assert companion_json_path(Path("sample.v2.csv")).name == "sample.v2.provenance.json"
    assert companion_json_path(Path("locs.csv")).name == "locs.provenance.json"


# 14. corrupt declared provenance raises ----------------------------------------------

def test_corrupt_declared_provenance_is_an_error_not_an_empty_document(tmp_path):
    with h5py.File(str(tmp_path / "bad.h5"), "w") as handle:
        handle.attrs["provenance"] = "{not json"
        handle.attrs["processing_history"] = json.dumps([{"operation": "crop"}])
    with pytest.raises(ProvenanceReadError, match="not valid JSON"):
        read_provenance(tmp_path / "bad.h5")


# 15. accepting drift makes it a run -------------------------------------------------

def test_allow_drift_relabels_the_report_as_a_run(registry, tmp_path):
    from imswitch.improcess.workflows.sources import fingerprint_of, open_source

    raw = _h5(tmp_path / "scan.h5")
    recorded = fingerprint_of(open_source(SourceSpec(path=str(raw))))
    wf = Workflow("d", [Source("raw", SourceSpec(path=str(raw), fingerprint=recorded)),
                        Reconstruct("rec", "view-only", inputs=["raw"])])
    _h5(raw, shape=(3, 8, 8))
    with run(wf, registry=registry, out_dir=tmp_path, mode="replay", allow_drift=True) as report:
        assert report.mode == "run" and report.warnings


# 16. a registry factory gives every row fresh plugins -----------------------------------

def test_batch_can_use_a_fresh_registry_per_row(tmp_path):
    made = []

    def factory():
        made.append(bootstrap_registry(user_plugins=False))
        return made[-1]

    wf = Workflow("b", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])])
    inputs = [_h5(tmp_path / f"{i}.h5") for i in range(2)]
    batch = run_over(wf, bindings_for_inputs(wf, inputs), registry=factory, out_dir=tmp_path / "o")
    assert batch.ok and len(made) == 2 and made[0] is not made[1]


# 17. deep graphs replay without recursion ---------------------------------------------

def test_a_long_chain_replays_iteratively(registry):
    from imswitch.improcess.model.save_protocol import ProvenanceDocument
    from imswitch.improcess.workflows.replay import workflow_from_provenance

    nodes = {"src": {"op": "source", "inputs": [], "outputs": ["data"],
                     "source": {"kind": "file", "path": "/x.h5", "dataset": "data", "fingerprint": {}}, "replayable": True},
             "rec": {"op": "reconstruct", "plugin_id": "view-only", "params": {}, "inputs": [{"node": "src", "port": "data"}],
                     "outputs": ["out"], "replayable": True}}
    previous = "rec"
    for index in range(3000):
        node_id = f"p{index}"
        nodes[node_id] = {"op": "process", "plugin_id": "filter", "params": {"radius": 1.0},
                          "inputs": [{"node": previous, "port": "out"}], "outputs": ["out"], "replayable": True}
        previous = node_id
    graph = {"schema": 1, "output": {"node": previous, "port": "out"}, "nodes": nodes}
    workflow = workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry).workflow
    assert len(workflow.steps) == 3003


# 18. cancellation attaches the report -------------------------------------------------

def test_cancellation_returns_the_report_with_its_open_sources(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("c", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    calls = iter([False, True])
    with pytest.raises(RunError, match="cancelled") as excinfo:
        run(wf, registry=registry, out_dir=tmp_path, cancel=lambda: next(calls))
    report = excinfo.value.report
    assert report.failed_step == "rec" and report._sources
    report.close()
    assert report._sources == []


# 19. writers save only the session's layers ------------------------------------------

def test_writer_formats_save_exactly_the_sessions_layers(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import Image, _controller, _endpoint
    from imswitch.improcess.model.napari_endpoints import InstalledPlugins, NapariWriterFormat

    controller, viewer, view, _ = _controller(tmp_path, _image())
    controller._installed = InstalledPlugins(
        plugin_names=controller._installed.plugin_names | {"writer-plugin"},
        widgets=controller._installed.widgets,
        reader_plugins=controller._installed.reader_plugins,
        writers=(NapariWriterFormat("writer-plugin", "writer-plugin.save", "Fancy", ("image",), (".fancy",)),),
    )
    viewer.add_layer(Image(np.zeros((2, 2)), {"name": "unrelated"}, "image"))
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    formats = controller.writerFormatsFor(session)
    assert [w.writer_id for w in formats] == ["writer-plugin.save"]
    saved = {}

    def fake_save_layers(path, layers, writer):
        saved["path"], saved["layers"], saved["writer"] = path, list(layers), writer
        return [path]

    controller.saveSessionLayers(session.uid, formats[0], tmp_path / "out.fancy", save_layers=fake_save_layers)
    assert saved["writer"].writer_id == "writer-plugin.save" and saved["layers"] == list(session.layers)
    assert len(saved["layers"]) == 1


# 20. the documented promotion exists ----------------------------------------------------

def test_points_table_promotes_to_localizations_with_an_explicit_mapping(registry):
    from imswitch.improcess.model.points_table_result import PointsTableResult

    table = PointsTableResult("pts", np.array([[1.0, 2.0], [3.0, 4.0]]),
                              properties={"frame": np.array([0, 1]), "brightness": np.array([100.0, 200.0])})
    processor = registry.get_processor("table-to-localizations")
    assert processor.accepts(table)
    result = processor.apply(table, {**type(processor).default_params(), "x_column": "x", "y_column": "y",
                                     "frame_column": "frame", "photons_column": "brightness",
                                     "unit": "px", "pixel_size_nm": 100.0})
    assert result.kind == "localization"
    np.testing.assert_allclose(result.locs["x_nm"], [200.0, 400.0])
    np.testing.assert_allclose(result.locs["y_nm"], [100.0, 300.0])
    assert list(result.locs["frame"]) == [0, 1]
    with pytest.raises(ValueError, match="not in the table"):
        processor.apply(table, {**type(processor).default_params(), "x_column": "nope"})

"""Regressions for the fifth review round: each test names the finding it pins."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.points_table_result import PointsTableResult  # noqa: E402
from imswitch.improcess.model.provenance import (  # noqa: E402
    attrs_digest,
    graph_of,
    make_node,
    output_node,
)
from imswitch.improcess.model.provenance_io import ProvenanceReadError, read_provenance  # noqa: E402
from imswitch.improcess.model.save_protocol import SaveError, companion_json_path  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    Workflow,
    bindings_for_inputs,
    bootstrap_registry,
    run,
    run_over,
    validate,
)
from imswitch.improcess.workflows.runner import render_save_path, source_stem_of  # noqa: E402


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
        # The axes are declared, not left to be guessed from the rank: a
        # file that says nothing reads as Frame/Y/X, never as channels.
        if len(shape) == 3:
            dataset.attrs["axes"] = "CYX"
    return path


def _image(name="img", shape=(4, 4)):
    return ArrayProcessingResult(name, np.random.default_rng(0).random(shape).astype(np.float32), ["Y", "X"])


# 1. a failed publish never leaves a published target ---------------------------------

def test_a_failure_after_the_primary_link_is_rolled_back(tmp_path, monkeypatch):
    """The reviewer's reproduction: the link succeeds, the step after it fails."""
    import imswitch.improcess.model.save_protocol as protocol

    real_unlink = os.unlink

    def unlink_fails_for_staged_primary(path, *args, **kwargs):
        if not args and not kwargs and Path(path).name == "x.ome.tif" and ".staging-" in str(path):
            raise OSError("cannot unlink the staged file")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(protocol.os, "unlink", unlink_fails_for_staged_primary)
    target = tmp_path / "x.ome.tif"
    _image().save(target)                     # an unlinkable staged copy is not a failed save
    assert target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".x.ome.tif.")]


def test_a_publish_that_raises_after_creating_the_target_rolls_it_back(tmp_path, monkeypatch):
    import imswitch.improcess.model.save_protocol as protocol
    from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult

    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    real_link = os.link

    def link_then_explode(src, dst):
        real_link(src, dst)
        if Path(dst).name == "d.ome.tif":
            raise OSError("disk went away right after the link")

    monkeypatch.setattr(protocol.os, "link", link_then_explode)
    with pytest.raises(OSError, match="disk went away"):
        result.save(tmp_path / "d.ome.tif", "tiff")
    assert not (tmp_path / "d.ome.tif").exists()          # the target the failed publish created
    assert not (tmp_path / "d.ome.drift.npy").exists()    # nor the companion published before it
    assert list(tmp_path.iterdir()) == []


def test_a_failed_backup_restore_keeps_the_backup_and_says_where(tmp_path, monkeypatch):
    import imswitch.improcess.model.save_protocol as protocol

    result = _image()
    target = tmp_path / "x.ome.tif"
    result.save(target)
    original = target.read_bytes()

    def publish_fails(staged, target_path, published):
        raise RuntimeError("late failure")            # after the existing file was moved aside

    real_replace = os.replace

    def restore_fails(src, dst):
        if ".backup-" in str(src):
            raise OSError("restore refused")
        real_replace(src, dst)

    monkeypatch.setattr(protocol, "_publish", publish_fails)
    monkeypatch.setattr(protocol.os, "replace", restore_fails)
    with pytest.raises(SaveError, match="could not be restored") as excinfo:
        result.save(target, "tiff", overwrite=True)
    backups = [p for p in tmp_path.iterdir() if p.name.startswith(".x.ome.tif.backup-")]
    assert len(backups) == 1 and str(backups[0]) in str(excinfo.value)
    assert (backups[0] / "x.ome.tif").read_bytes() == original   # the only copy is kept


# 2./3. live provenance fails closed, and the first stack counts -------------------------

def test_a_live_result_whose_provenance_cannot_be_recorded_is_a_failure(monkeypatch):
    from imswitch.improcess._test.test_reconstruction_provenance_paths import _stream_worker
    from imswitch.improcess.reconstructors import run as run_module
    from imswitch.improcess.reconstructors.base import Chunk

    worker, _ = _stream_worker(expected=4)
    monkeypatch.setattr(run_module, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no record")))
    finished, failed = [], []
    worker.sigStackFinished.connect(finished.append)
    worker.sigFailed.connect(failed.append)
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 2, 4))
    worker.finalize()
    assert finished == [] and failed and "provenance" in failed[0]


def test_the_initial_stack_counts_towards_the_committed_frames():
    from imswitch.improcess._test.test_reconstruction_provenance_paths import _stream_worker

    worker, _ = _stream_worker(expected=2)        # the two init frames are the whole acquisition
    finals = []
    worker.sigStackFinished.connect(finals.append)
    worker.finalize()                              # no chunk ever arrives after begin()
    assert output_node(finals[0])["completion"] == {"status": "complete", "frames_committed": 2, "expected_frames": 2}


# 4. codec output is checked as a mapping ---------------------------------------------

def test_a_codec_returning_a_non_string_key_is_not_replayable():
    plugin = SimpleNamespace(id="p", name="P", version="1", params_version=1,
                             encode_params=lambda params: ({1: "integer-key", "ok": 2}, []))
    node_id, node = make_node("process", plugin=plugin, params={"1": "integer-key", "ok": 2},
                              inputs=[], outputs=["out"])
    assert node["replayable"] is False and any("not lossless JSON" in r for r in node["reasons"])
    assert all(isinstance(key, str) for key in node["params"])
    assert json.loads(json.dumps(node["params"])) == node["params"]


# 5. hidden merge/combine parameters are declared ---------------------------------------

def test_channel_merge_and_stack_combine_declare_their_dialog_parameters(registry, tmp_path):
    from imswitch.improcess.processors.channel_merge import ChannelMergeProcessor
    from imswitch.improcess.processors.combine import StackCombineProcessor

    assert ChannelMergeProcessor.param_keys() == {"name", "axis_label"}
    assert StackCombineProcessor.param_keys() == {"mode", "join_axis", "name", "axis_label"}
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("m", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("a", "projection", {"axis": "C", "mode": "max"}, inputs=["rec"]),
        Process("b", "projection", {"axis": "C", "mode": "mean"}, inputs=["rec"]),
        Process("merge", "channel-merge", {"name": "two", "axis_label": "C"}, inputs=["a", "b"]),
        Process("cat", "stack-combine", {"mode": "concatenate", "join_axis": 0, "name": "joined"}, inputs=["a", "b"]),
    ])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        assert report.result("merge").name == "two" and report.result("merge").data.shape[0] == 2
        assert report.result("cat").name == "joined" and report.result("cat").data.shape[0] == 16


# 6. dotted source names stay distinct -------------------------------------------------

def test_dotted_source_names_keep_their_inner_dots():
    assert source_stem_of("sample.v1.h5") == "sample.v1"
    assert source_stem_of("sample.v2.h5") == "sample.v2"
    assert source_stem_of("/x/cell.ome.tif") == "cell" and source_stem_of("run.zarr") == "run"
    assert source_stem_of("odd.name.unknownext") == "odd.name"
    step = Save("save", input="rec", fmt="tiff")
    paths = {
        render_save_path(step, out_dir=Path("/o"), source_stem=source_stem_of(name), result=_image(), input_step="rec")
        for name in ("sample.v1.h5", "sample.v2.h5")
    }
    assert len(paths) == 2


# 7. points to localizations: per-axis physical coordinates --------------------------------

def test_pixel_unit_points_scale_z_by_the_z_step(registry):
    table = PointsTableResult("pts", np.array([[2.0, 3.0, 4.0]]), axis_names=["z", "y", "x"])
    processor = registry.get_processor("table-to-localizations")
    params = {**type(processor).default_params(), "x_column": "x", "y_column": "y", "z_column": "z",
              "unit": "px", "pixel_size_nm": 100.0, "z_step_nm": 500.0}
    result = processor.apply(table, params)
    assert (float(result.locs["x_nm"][0]), float(result.locs["y_nm"][0]), float(result.locs["z_nm"][0])) == (400.0, 300.0, 1000.0)
    with pytest.raises(ValueError, match="z_step_nm"):
        processor.apply(table, {**params, "z_step_nm": 0.0})


def test_table_unit_uses_the_tables_own_per_axis_scale(registry):
    processor = registry.get_processor("table-to-localizations")
    in_um = PointsTableResult("pts", np.array([[2.0, 3.0, 4.0]]), axis_names=["z", "y", "x"],
                              coordinate_scale=[0.5, 0.1, 0.1], scale_unit="um")
    result = processor.apply(in_um, {**type(processor).default_params(), "x_column": "x", "y_column": "y",
                                     "z_column": "z", "unit": "table", "pixel_size_nm": 100.0})
    assert (float(result.locs["x_nm"][0]), float(result.locs["y_nm"][0]), float(result.locs["z_nm"][0])) == (400.0, 300.0, 1000.0)
    in_px = PointsTableResult("pts", np.array([[2.0, 3.0, 4.0]]), axis_names=["z", "y", "x"],
                              coordinate_scale=[5.0, 1.0, 1.0], scale_unit="px")
    result = processor.apply(in_px, {**type(processor).default_params(), "x_column": "x", "y_column": "y",
                                     "z_column": "z", "unit": "table", "pixel_size_nm": 100.0, "z_step_nm": 500.0})
    assert float(result.locs["z_nm"][0]) == 5000.0
    plain = SimpleNamespace(name="t", table_columns=lambda: ["x", "y"], table_records=lambda: [{"x": 1.0, "y": 2.0}])
    with pytest.raises(ValueError, match="unit 'table'"):
        processor.apply(plain, {**type(processor).default_params(), "unit": "table", "x_column": "x", "y_column": "y"})


def test_an_imported_points_layer_bakes_its_translation_and_refuses_rotation():
    from imswitch.improcess.model.napari_import import NotImportable, import_layer, snapshot_layer

    def layer(**overrides):
        base = dict(name="pts", data=np.array([[1.0, 2.0], [3.0, 4.0]]), ndim=2, scale=np.array([1.0, 0.5]),
                    translate=np.array([0.0, 0.0]), rotate=np.eye(2), shear=np.zeros(1),
                    affine=SimpleNamespace(affine_matrix=np.eye(3)), metadata={}, properties={}, units=None)
        base.update(overrides)
        return SimpleNamespace(**base)

    class Points(SimpleNamespace):
        pass

    shifted = Points(**vars(layer(translate=np.array([10.0, 3.0]))))
    imported = import_layer(snapshot_layer(shifted), None, plugin_name="p", widget_name="w")
    np.testing.assert_allclose(imported.result.coordinates, [[11.0, 8.0], [13.0, 10.0]])   # translate / scale
    assert imported.result.coordinate_scale == [1.0, 0.5]
    assert imported.result.metadata["transform_baked"]["translate"] == [10.0, 3.0]
    rotated = Points(**vars(layer(rotate=np.array([[0.0, -1.0], [1.0, 0.0]]))))
    with pytest.raises(NotImportable, match="rotated"):
        import_layer(snapshot_layer(rotated), None, plugin_name="p", widget_name="w")


# 8. attribution comes from napari's record, and the dialog drops a stale mapping ----------

def test_a_layer_is_attributed_only_when_napari_says_the_session_made_it(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import Labels, _controller
    from imswitch.improcess.model.napari_endpoints import NapariEndpoint, OutputMapping

    controller, viewer, _, _ = _controller(tmp_path, _image())
    endpoint = NapariEndpoint(id="napari-skimage:threshold", label="T", lane="dock", plugin_name="napari-skimage",
                              widget_name="Automated Threshold", kinds=("image",), verified=True,
                              output_mappings=(OutputMapping("*", "labels", "labels", preserves_grid=True),))
    session = controller.sendTo(endpoint)
    by_widget = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "a"}, "labels"))
    by_widget.source = SimpleNamespace(widget=session.widget, parent=None)
    derived = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "b"}, "labels"))
    derived.source = SimpleNamespace(widget=None, parent=lambda: session.layers[0])   # napari keeps a weakref
    stranger = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "c"}, "labels"))
    stranger.source = SimpleNamespace(widget=object(), parent=None)
    no_record = viewer.add_layer(Labels(np.ones((4, 4), np.int32), {"name": "d"}, "labels"))
    suggestions = controller._mappingSuggestions([by_widget, derived, stranger, no_record])
    assert set(suggestions) == {id(by_widget), id(derived)}
    candidates = controller._mappingCandidates([stranger, no_record])
    assert set(candidates) == {id(stranger), id(no_record)}     # offered, never assumed


def test_the_import_dialog_drops_the_mapping_when_the_result_changes(qapp):
    from imswitch.improcess.model.napari_endpoints import NapariEndpoint, OutputMapping
    from imswitch.improcess.view.NapariImportDialog import NapariImportDialog

    mapping = OutputMapping("*", "labels", "labels", preserves_grid=True, label="mask")
    endpoint = NapariEndpoint(id="e", label="E", lane="dock", plugin_name="p", widget_name="W",
                              kinds=("image",), verified=True, output_mappings=(mapping,))
    layer = SimpleNamespace(name="mask")
    first, second = _image("first"), _image("second")
    dialog = NapariImportDialog(
        None, [layer], [first, second],
        suggestions={id(layer): (endpoint, mapping, first.result_uid)},
        candidates={id(layer): [(endpoint, mapping, first.result_uid)]},
    )
    assert dialog.selection()[1] is first and dialog.selection()[3] is endpoint
    dialog.resultCombo.setCurrentIndex(1)                # the user picks another source result
    assert dialog.selection()[1] is second and dialog.selection()[3] is None
    dialog.mappingCombo.setCurrentIndex(1)               # choosing the mapping again re-selects its result
    assert dialog.selection()[1] is first and dialog.selection()[3] is endpoint


# 9. writer constraints and dispatch ----------------------------------------------------

@pytest.mark.parametrize("constraints, layers, accepted", [
    (("image+",), ["image"], True),
    (("image+",), ["image", "image"], True),
    (("image",), ["image", "image"], False),
    (("image",), ["image"], True),
    (("image?", "labels"), ["labels"], True),
    (("image?", "labels"), ["image", "image", "labels"], False),
    (("image{2}",), ["image", "image"], True),
    (("image{2}",), ["image"], False),
    (("image{1,3}", "points*"), ["image", "points", "points", "points"], True),
    (("image*",), ["labels"], False),
    (("image*",), [], False),
])
def test_writer_constraints_follow_npe2_multiplicities(constraints, layers, accepted):
    from imswitch.improcess.model.napari_endpoints import NapariWriterFormat, writer_accepts

    writer = NapariWriterFormat("p", "p.w", "W", constraints, (".x",))
    assert writer_accepts(writer, layers) is accepted


def test_the_chosen_writer_is_dispatched_by_its_id_not_the_plugin(monkeypatch):
    from imswitch.improcess.controller import NapariEndpointController as module
    from imswitch.improcess.model.napari_endpoints import NapariWriterFormat

    lossy = SimpleNamespace(command="p.lossy")
    lossless = SimpleNamespace(command="p.lossless")
    fake_manager = SimpleNamespace(iter_compatible_writers=lambda types: [lossy, lossless])
    calls = {}

    def fake_save_layers(path, layers, plugin=None, _writer=None):
        calls.update(path=path, plugin=plugin, writer=_writer)
        return [path]

    monkeypatch.setattr(module, "_npe2_manager", lambda: fake_manager)
    monkeypatch.setattr(module, "_napari_save_layers", lambda: fake_save_layers)
    module._save_with_writer("/tmp/x.y", [SimpleNamespace()], NapariWriterFormat("p", "p.lossless", "L", ("image",), (".y",)))
    assert calls["writer"] is lossless and calls["plugin"] == "p"
    with pytest.raises(RuntimeError, match="not available"):
        module._save_with_writer("/tmp/x.y", [SimpleNamespace()], NapariWriterFormat("p", "p.gone", "G", ("image",), (".y",)))


# 10. a reader that fails part-way leaves nothing behind ------------------------------------

def test_a_reader_that_adds_a_layer_and_then_fails_is_rolled_back(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _READER, Image, _controller

    controller, viewer, _, _ = _controller(tmp_path, _image())
    kept = viewer.add_layer(Image(np.zeros((2, 2)), {"name": "kept"}, "image"))

    def open_then_fail(path, plugin=None):
        viewer.layers.append(Image(np.zeros((2, 2)), {"name": "half"}, "image"))
        raise RuntimeError("second file part unreadable")

    viewer.open = open_then_fail
    session = controller.sendTo(_READER)
    assert session.state == "failed" and "unreadable" in session.error
    assert list(viewer.layers) == [kept]


# 11. failed batch rows list what they wrote -------------------------------------------------

def test_a_failed_row_lists_the_files_it_published_before_failing(registry, tmp_path):
    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("p", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Save("save", input="rec", fmt="tiff"),
        Process("bad", "table-to-localizations", inputs=["rec"]),   # refuses an image at run time
    ])
    batch = run_over(wf, bindings_for_inputs(wf, [raw]), registry=registry, out_dir=tmp_path / "out")
    row = batch.rows[0]
    assert not row.ok and row.failed_step == "bad"
    assert row.partial and row.files == [str(tmp_path / "out" / "a_save.ome.tif")]
    assert Path(row.files[0]).exists()
    text = batch.write_summary(tmp_path / "out" / "summary.csv").read_text()
    assert "partial" in text.splitlines()[0] and "a_save.ome.tif" in text


# 12. a malformed declared companion is an error ---------------------------------------------

def test_a_malformed_provenance_companion_raises(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("x,y\n1,2\n")
    companion_json_path(csv_path).write_text("{not json")
    with pytest.raises(ProvenanceReadError, match="not valid JSON"):
        read_provenance(csv_path)
    companion_json_path(csv_path).write_text("[1, 2]")
    with pytest.raises(ProvenanceReadError, match="not a JSON object"):
        read_provenance(csv_path)
    with pytest.raises(ProvenanceReadError):
        read_provenance(companion_json_path(csv_path))
    companion_json_path(csv_path).unlink()
    assert read_provenance(csv_path).graph is None        # absent is still simply empty


# secondary ------------------------------------------------------------------------------

def test_the_cli_gives_every_batch_row_a_fresh_registry(tmp_path, monkeypatch):
    from imswitch.improcess.workflows import __main__ as cli

    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])])
    wf_path = wf.save(tmp_path / "w.yaml")
    seen = {}

    def fake_run_over(workflow, bindings_list, *, registry, **kwargs):
        seen["registry"] = registry
        return SimpleNamespace(rows=[], failures=[], ok=True, write_summary=lambda p: Path(p))

    monkeypatch.setattr("imswitch.improcess.workflows.batch.run_over", fake_run_over)
    assert cli.main(["--no-user-plugins", "run", str(wf_path), "--input", str(raw), "--out", str(tmp_path / "o")]) == 0
    assert callable(seen["registry"]) and not hasattr(seen["registry"], "get_processor")


def test_gui_retained_sources_are_released_when_their_results_are_gone(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    raw = _h5(tmp_path / "scan.h5")
    loaded = []
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    controller = WorkflowController(comm, view, SimpleNamespace(getActiveResult=lambda: None))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    assert len(controller._retained) == 1
    assert controller.releaseUnusedSources() == 0          # its result is still loaded
    loaded.clear()                                          # the user removed it from the list
    assert controller.releaseUnusedSources() == 1 and controller._retained == []
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    controller.shutdown()
    assert controller._retained == []      # a loaded result goes away with the window; no lease at shutdown


def test_closing_an_exporting_session_interrupts_and_shutdown_joins_the_worker(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _READER, _controller

    class Thread:
        def __init__(self):
            self.interrupted = False
            self.waited = None
            self.running = True

        def requestInterruption(self):
            self.interrupted = True

        def isRunning(self):
            return self.running

        def quit(self):
            pass

        def wait(self, ms):
            self.waited = ms
            self.running = False
            return True

    thread = Thread()

    def hanging_runner(uid, job, on_done, on_failed):
        return (thread, object())

    controller, _, _, _ = _controller(tmp_path, _image())
    controller._export_runner = hanging_runner
    session = controller.sendTo(_READER)
    controller._keepalive.append((thread, object()))
    controller.closeSession(session.uid)
    assert session.cancelled and thread.interrupted
    assert controller.closeAll(wait_ms=123) is True and thread.waited == 123 and not thread.running


def test_attribute_digest_sees_every_element():
    long_a = {"list": list(range(100)), "name": "x"}
    long_b = {"list": list(range(100)), "name": "x"}
    long_b["list"][65] = -1                                  # past json_safe's 64-item cut
    assert attrs_digest(long_a) != attrs_digest(long_b)
    assert attrs_digest({"arr": np.arange(200)}) != attrs_digest({"arr": np.r_[np.arange(199), 0]})
    assert attrs_digest({"s": "a" * 5000}) != attrs_digest({"s": "a" * 4999 + "b"})
    assert attrs_digest(long_a) == attrs_digest(dict(reversed(list(long_a.items()))))


def test_a_run_report_fingerprint_uses_the_full_digest(registry, tmp_path):
    from imswitch.improcess.workflows.sources import fingerprint_of, open_source, SourceSpec

    a = tmp_path / "a.h5"
    with h5py.File(str(a), "w") as handle:
        handle.create_dataset("data", data=np.zeros((2, 4, 4), np.float32)).attrs["long"] = np.arange(100)
    first = fingerprint_of(open_source(SourceSpec(path=str(a))))["attrs_digest"]
    with h5py.File(str(a), "r+") as handle:
        values = np.arange(100)
        values[65] = -1
        handle["data"].attrs["long"] = values
    second = fingerprint_of(open_source(SourceSpec(path=str(a))))["attrs_digest"]
    assert first != second


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

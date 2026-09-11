"""Regressions for the sixth review round: each test names the finding it pins."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.napari_import import (  # noqa: E402
    LayerSnapshot,
    NotImportable,
    import_layer,
    normalized_units,
    snapshot_layer,
    transform_problems,
)
from imswitch.improcess.model.provenance import attrs_digest  # noqa: E402
from imswitch.improcess.model.provenance_io import ProvenanceReadError, read_provenance  # noqa: E402
from imswitch.improcess.model.save_protocol import SaveError, companion_json_path  # noqa: E402
from imswitch.improcess.workflows import Reconstruct, Source, Workflow, bootstrap_registry, run  # noqa: E402


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


def _image(name="img", shape=(4, 4)):
    return ArrayProcessingResult(name, np.random.default_rng(0).random(shape).astype(np.float32), ["Y", "X"])


# 1. a rollback that cannot clean up says what is left ------------------------------------

def test_a_rollback_that_leaves_files_names_every_leftover(tmp_path, monkeypatch):
    import imswitch.improcess.model.save_protocol as protocol
    from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult

    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    real_link, real_unlink = os.link, os.unlink

    def link_fails_for_primary(src, dst):
        if Path(dst).name == "d.ome.tif":
            raise OSError("no space for the primary")
        real_link(src, dst)

    def unlink_refuses_the_published_companion(path, *args, **kwargs):
        if not args and not kwargs and Path(path) == tmp_path / "d.ome.drift.npy":
            raise OSError("companion is busy")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(protocol.os, "link", link_fails_for_primary)
    monkeypatch.setattr(protocol.os, "unlink", unlink_refuses_the_published_companion)
    with pytest.raises(SaveError, match="left behind") as excinfo:
        result.save(tmp_path / "d.ome.tif", "tiff")
    message = str(excinfo.value)
    assert "no space for the primary" in message
    assert str(tmp_path / "d.ome.drift.npy") in message and "companion is busy" in message
    assert (tmp_path / "d.ome.drift.npy").exists()          # reported, not hidden


# 2. endpoint sessions lease the sources their layers read -----------------------------------

def test_retained_sources_stay_open_while_an_endpoint_session_holds_the_result(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    raw = _h5(tmp_path / "scan.h5")
    loaded, held = [], set()
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    controller = WorkflowController(comm, view, SimpleNamespace(getActiveResult=lambda: None))
    controller.addHolder(lambda: set(held))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    result = loaded[0][1]
    held.add(result.result_uid)                          # an endpoint session shows its layers
    loaded.clear()                                        # the user removes it from the list
    assert controller.releaseUnusedSources() == 0
    assert np.asarray(result.data).shape == (2, 8, 8)     # the lazy view still reads
    held.clear()                                          # the session closes
    assert controller.releaseUnusedSources() == 1


def test_a_derived_result_keeps_its_parents_source_through_lineage(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    raw = _h5(tmp_path / "scan.h5")
    loaded = []
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    controller = WorkflowController(comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None),
                                    SimpleNamespace(getActiveResult=lambda: None))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    parent = loaded.pop()[1]
    child = SimpleNamespace(result_uid="child", lineage=(parent.result_uid,))
    loaded.append(("child", child))
    assert controller.releaseUnusedSources() == 0


# 3. a running workflow is cancelled and joined at shutdown -----------------------------------

def test_the_run_worker_observes_cancellation_between_steps(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import _RunWorker

    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    worker = _RunWorker(wf, registry, tmp_path)
    failed, finished = [], []
    worker.sigFailed.connect(lambda message, report: failed.append((message, report)))
    worker.sigFinished.connect(finished.append)
    worker.cancel()
    worker.run()
    assert finished == [] and failed and "cancelled" in failed[0][0]
    failed[0][1].close()


def test_shutdown_cancels_and_joins_a_running_workflow_before_closing_sources():
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    events = []

    class Thread:
        running = True

        def requestInterruption(self):
            events.append("interrupt")

        def quit(self):
            events.append("quit")

        def isRunning(self):
            return self.running

        def wait(self, ms):
            events.append(("wait", ms))
            self.running = False
            return True

    class Worker:
        def cancel(self):
            events.append("cancel")

    controller = WorkflowController(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    controller._thread, controller._worker = Thread(), Worker()
    controller._retained = [(frozenset({"r"}), [SimpleNamespace(checkAndUnloadData=lambda: events.append("close"))])]
    assert controller.shutdown(wait_ms=77) is True
    assert events == ["cancel", "interrupt", "quit", ("wait", 77), "close"] and controller._retained == []

    class StuckThread(Thread):
        def wait(self, ms):
            return False

    controller._thread, controller._worker = StuckThread(), Worker()
    controller._retained = [(frozenset({"r"}), [SimpleNamespace(checkAndUnloadData=lambda: events.append("close2"))])]
    assert controller.shutdown(wait_ms=1) is False
    assert "close2" not in events and controller._retained     # sources stay open under a running step


# 4./5. points transforms and per-axis units ------------------------------------------------

def _points(**overrides):
    base = dict(name="pts", data=np.array([[1.0, 2.0], [3.0, 4.0]]), ndim=2, scale=np.array([1.0, 1.0]),
                translate=np.array([0.0, 0.0]), rotate=np.eye(2), shear=np.zeros(1),
                affine=SimpleNamespace(affine_matrix=np.eye(3)), metadata={}, properties={}, units=None)
    base.update(overrides)

    class Points(SimpleNamespace):
        pass

    return Points(**base)


def test_a_translation_cannot_hide_a_rotation():
    angle = np.deg2rad(45.0)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    layer = _points(translate=np.array([10.0, 20.0]), rotate=rotation)
    snapshot = snapshot_layer(layer)
    problems = transform_problems(snapshot)
    assert any("translated" in p for p in problems) and any("rotated" in p for p in problems)
    with pytest.raises(NotImportable, match="rotated"):
        import_layer(snapshot, None, plugin_name="p", widget_name="w")
    sheared = _points(translate=np.array([1.0, 0.0]), shear=np.array([0.5]))
    with pytest.raises(NotImportable, match="sheared"):
        import_layer(snapshot_layer(sheared), None, plugin_name="p", widget_name="w")


def test_per_axis_length_units_are_converted_onto_one_unit():
    layer = _points(scale=np.array([1.0, 2.0]), units=("micrometer", "nanometer"))
    imported = import_layer(snapshot_layer(layer), None, plugin_name="p", widget_name="w")
    assert imported.result.scale_unit == "nm" and imported.result.coordinate_scale == [1000.0, 2.0]
    snap = LayerSnapshot(name="i", layer_type="image", data=np.zeros((2, 2), np.float32), ndim=2,
                         scale=(0.5, 0.5), translate=(0.0, 0.0), rotate=np.eye(2), shear=np.zeros(1),
                         affine=np.eye(3), metadata={}, units=("micrometer", "micrometer"))
    unit, scales = normalized_units(snap, None)
    assert (unit, scales) == ("um", [0.5, 0.5])
    result = import_layer(snap, None, plugin_name="p", widget_name="w").result
    assert result.scale_unit == "um" and list(result.axis_scales) == [0.5, 0.5]
    mixed = LayerSnapshot(name="i", layer_type="image", data=np.zeros((2, 2), np.float32), ndim=2,
                          scale=(0.5, 0.5), translate=(0.0, 0.0), rotate=np.eye(2), shear=np.zeros(1),
                          affine=np.eye(3), metadata={}, units=("second", "micrometer"))
    with pytest.raises(NotImportable, match="not all lengths"):
        import_layer(mixed, None, plugin_name="p", widget_name="w")
    same_non_length = LayerSnapshot(name="i", layer_type="image", data=np.zeros((2, 2), np.float32), ndim=2,
                                    scale=(1.0, 1.0), translate=(0.0, 0.0), rotate=np.eye(2), shear=np.zeros(1),
                                    affine=np.eye(3), metadata={}, units=("pixel", "pixel"))
    assert normalized_units(same_non_length, None) == ("px", [1.0, 1.0])


def test_a_layer_in_another_unit_than_its_source_gets_a_fresh_grid():
    source = ArrayProcessingResult("src", np.zeros((2, 2), np.float32), ["Y", "X"], axis_scales=[0.5, 0.5], scale_unit="um")
    snap = LayerSnapshot(name="i", layer_type="labels", data=np.zeros((2, 2), np.int32), ndim=2,
                         scale=(0.5, 0.5), translate=(0.0, 0.0), rotate=np.eye(2), shear=np.zeros(1),
                         affine=np.eye(3), metadata={}, units=("nanometer", "nanometer"))
    imported = import_layer(snap, source, plugin_name="p", widget_name="w", preserves_grid=True)
    assert imported.grid == "fresh" and "unit" in imported.reason


# 6. export cancellation keeps a tombstone and cleans up ------------------------------------

def test_a_cancelled_export_keeps_its_tombstone_until_the_worker_returns(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _READER, _controller
    from imswitch.improcess.controller import NapariEndpointController as module

    class Thread:
        def __init__(self):
            self.running, self.interrupted = True, False

        def requestInterruption(self):
            self.interrupted = True

        def isRunning(self):
            return self.running

        def quit(self):
            pass

        def wait(self, ms):
            return False                              # does not finish in time

    thread, pending = Thread(), {}

    def hanging_runner(uid, job, on_done, on_failed):
        pending["finish"] = lambda: on_done(uid, job())
        return (thread, object())

    controller, viewer, _, _ = _controller(tmp_path, _image())
    controller._export_runner = hanging_runner
    session = controller.sendTo(_READER)
    controller._keepalive.append(session.worker)
    assert controller.closeAll(wait_ms=1) is False
    assert session.cancelled and thread.interrupted
    assert controller.registry.get(session.uid) is session          # tombstone kept
    assert session.worker in module._ORPHANED_EXPORTS                # ownership retained
    thread.running = False
    pending["finish"]()                                              # the worker comes back late
    assert controller.registry.get(session.uid) is None and viewer.opened == []
    module._ORPHANED_EXPORTS.clear()


def test_a_failed_export_after_close_cleans_the_directory_it_recreated(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _READER, _controller

    controller, _, _, _ = _controller(tmp_path, _image())
    controller._export_runner = lambda uid, job, on_done, on_failed: (SimpleNamespace(isRunning=lambda: True), None)
    session = controller.sendTo(_READER)
    directory = session.temp_dir
    controller.closeSession(session.uid)
    assert not directory.exists()
    directory.mkdir()
    (directory / "export.h5").write_bytes(b"late")
    controller._onExportFailed(session.uid, "disk full", directory)
    assert not directory.exists() and controller.registry.get(session.uid) is None


def test_the_export_worker_reports_its_directory_on_failure(tmp_path):
    from imswitch.improcess.controller.NapariEndpointController import _ExportWorker

    def job():
        raise RuntimeError("boom")

    worker = _ExportWorker("s", job, tmp_path)
    failures = []
    worker.sigFailed.connect(lambda uid, message, directory: failures.append((uid, message, directory)))
    worker.run()
    assert failures == [("s", "boom", tmp_path)]


# 7. an orphan mapping cannot attach to a visible result -----------------------------------

def test_a_mapping_whose_result_is_gone_is_neither_offered_nor_returned(qapp):
    from imswitch.improcess.model.napari_endpoints import NapariEndpoint, OutputMapping
    from imswitch.improcess.view.NapariImportDialog import NapariImportDialog

    mapping = OutputMapping("*", "labels", "labels", preserves_grid=True, label="mask")
    endpoint = NapariEndpoint(id="e", label="E", lane="dock", plugin_name="p", widget_name="W",
                              kinds=("image",), verified=True, output_mappings=(mapping,))
    layer = SimpleNamespace(name="mask")
    visible = _image("visible")
    dialog = NapariImportDialog(
        None, [layer], [visible],
        suggestions={id(layer): (endpoint, mapping, "gone-uid")},
        candidates={id(layer): [(endpoint, mapping, "gone-uid")]},
    )
    assert dialog.mappingCombo.count() == 1                     # only "none"
    assert dialog.selection()[3] is None
    dialog._mappingChoices.append((endpoint, mapping, "gone-uid"))   # even if one slipped in
    dialog.mappingCombo.addItem("orphan")
    dialog.mappingCombo.setCurrentIndex(1)
    assert dialog.selection()[1] is visible and dialog.selection()[3] is None


# 8. structurally corrupt provenance is corrupt, not absent ---------------------------------

def test_declared_provenance_with_wrongly_typed_fields_raises(tmp_path):
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("x\n1\n")
    for payload, field in (({"schema": 2, "graph": []}, "graph"), ({"schema": "1"}, "schema"),
                           ({"artifact": 3}, "artifact"), ({"processing_history": {}}, "processing_history")):
        companion_json_path(csv_path).write_text(json.dumps(payload))
        with pytest.raises(ProvenanceReadError, match=field):
            read_provenance(csv_path)
    with h5py.File(str(tmp_path / "e.h5"), "w") as handle:
        handle.attrs["provenance"] = json.dumps({"schema": 1, "graph": [1, 2]})
    with pytest.raises(ProvenanceReadError, match="graph"):
        read_provenance(tmp_path / "e.h5")


# 9. a detached viewer that cannot open is closed -------------------------------------------

def test_a_failed_detached_open_closes_the_viewer_it_made(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _DETACHED, _DetachedViewer, _controller

    made = []

    class Broken(_DetachedViewer):
        def __init__(self):
            super().__init__()
            made.append(self)

        def open(self, path, plugin=None):
            raise RuntimeError("reader crashed")

    controller, _, _, _ = _controller(tmp_path, _image(), detached_factory=Broken)
    session = controller.sendTo(_DETACHED)
    assert session.state == "failed" and made and made[0].closed


# 10. bytes attributes hash losslessly --------------------------------------------------------

def test_byte_attributes_are_hashed_losslessly():
    assert attrs_digest({"b": b"\xff"}) != attrs_digest({"b": b"\xfe"})
    assert attrs_digest({"b": b"\xff"}) != attrs_digest({"b": "�"})


# 11. the CLI discovers plugins once per row, not once more up front ----------------------------

def test_cli_run_does_not_build_an_unused_registry(tmp_path, monkeypatch):
    from imswitch.improcess.workflows import __main__ as cli

    raw = _h5(tmp_path / "a.h5")
    wf_path = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])]).save(tmp_path / "w.yaml")
    calls = []
    monkeypatch.setattr(cli, "_registry", lambda args: calls.append(1))
    monkeypatch.setattr("imswitch.improcess.workflows.batch.run_over", lambda *a, **k: SimpleNamespace(
        rows=[], failures=[], ok=True, write_summary=lambda p: Path(p)))
    assert cli.main(["run", str(wf_path), "--input", str(raw), "--out", str(tmp_path / "o")]) == 0
    assert calls == []                                            # only the per-row factory would call it


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

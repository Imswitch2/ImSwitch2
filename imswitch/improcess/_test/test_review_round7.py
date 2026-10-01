"""Regressions for the seventh review round: each test names the finding it pins."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore  # noqa: E402

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.napari_endpoints import NapariEndpoint  # noqa: E402
from imswitch.improcess.model.napari_layers import layer_units  # noqa: E402
from imswitch.improcess.model.napari_sessions import SessionRegistry  # noqa: E402
from imswitch.improcess.model.provenance import attrs_digest  # noqa: E402
from imswitch.improcess.model.provenance_io import ProvenanceReadError, read_provenance  # noqa: E402
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


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


# 1. rollback cleanup works on every supported Python -------------------------------------------

def test_directory_cleanup_uses_the_portable_rmtree_callback(tmp_path, monkeypatch):
    import imswitch.improcess.model.save_protocol as protocol

    seen = {}

    def rmtree_3_10(path, ignore_errors=False, onerror=None, **kwargs):
        seen["kwargs"] = set(kwargs)
        assert onerror is not None, "cleanup must use onerror (onexc is Python 3.12+)"
        onerror(os.rmdir, str(path), (OSError, OSError("busy"), None))

    monkeypatch.setattr(protocol.shutil, "rmtree", rmtree_3_10)
    directory = tmp_path / "dir"
    directory.mkdir()
    problem = protocol._remove(directory)
    assert "busy" in problem and seen["kwargs"] == set()
    source = Path(protocol.__file__).read_text()
    assert "onexc" not in source


# 2. shutdown stops a real QThread whose quit is queued behind the blocked GUI thread ---------------

def _running_controller(monkeypatch, registry, tmp_path, *, stops_on_cancel: bool):
    from imswitch.improcess.controller import WorkflowController as module
    from imswitch.improcess.controller.WorkflowController import WorkflowController, _RunWorker
    from imswitch.improcess.workflows import runner

    started = {"running": False}

    def slow_run(workflow, *, registry, out_dir, cancel=None, **kwargs):
        started["running"] = True
        deadline = time.monotonic() + (10.0 if stops_on_cancel else 0.6)
        while time.monotonic() < deadline:
            if stops_on_cancel and cancel is not None and cancel():
                raise runner.RunError("cancelled")
            time.sleep(0.01)
        return SimpleNamespace(results={}, receipts=[], warnings=[], detach_sources=lambda: [], close=lambda: None)

    monkeypatch.setattr(runner, "run", slow_run)
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    controller = WorkflowController(SimpleNamespace(), view, SimpleNamespace(getActiveResult=lambda: None))
    # Wired exactly as runWorkflow wires it: the completion signals queue
    # thread.quit onto the GUI thread.
    controller._thread = QtCore.QThread()
    controller._worker = _RunWorker(wf, registry, tmp_path)
    controller._worker.moveToThread(controller._thread)
    controller._thread.started.connect(controller._worker.run)
    for signal in (controller._worker.sigFinished, controller._worker.sigFailed):
        signal.connect(controller._thread.quit)
    controller._thread.start()
    for _ in range(500):
        if started["running"]:
            break
        time.sleep(0.01)
    assert started["running"]
    return controller, module


def test_shutdown_joins_a_real_workflow_thread_without_the_gui_event_loop(monkeypatch, registry, tmp_path):
    controller, _module = _running_controller(monkeypatch, registry, tmp_path, stops_on_cancel=True)
    thread = controller._thread
    assert controller.shutdown(wait_ms=5000) is True
    assert not thread.isRunning()


def test_a_run_that_ignores_cancellation_is_parked_not_destroyed(monkeypatch, registry, tmp_path):
    controller, module = _running_controller(monkeypatch, registry, tmp_path, stops_on_cancel=False)
    thread, worker = controller._thread, controller._worker
    controller._retained = [(frozenset({"r"}), [SimpleNamespace(checkAndUnloadData=lambda: None)])]
    assert controller.shutdown(wait_ms=20) is False
    assert (thread, worker) in module._ORPHANED_RUNS and controller._retained
    assert thread.wait(5000)                                # it finishes on its own
    controller._clear()
    assert (thread, worker) not in module._ORPHANED_RUNS


# 3. leases cover lineage and cancelled exports --------------------------------------------------

def _endpoint():
    return NapariEndpoint(id="p:r", label="P", lane="reader", plugin_name="p", reader_plugin="p",
                          export_format="hdf5", kinds=("image",), verified=True)


def test_a_session_on_a_derived_result_leases_its_ancestors():
    registry = SessionRegistry()
    child = SimpleNamespace(result_uid="child", name="c", lineage=("workflow-result",))
    session = registry.open(_endpoint(), child)
    registry.mark_open(session, layers=[object()])
    assert {"child", "workflow-result"} <= registry.held_result_uids()


def test_a_cancelled_export_keeps_its_lease_until_the_worker_reports_back():
    registry = SessionRegistry()
    session = registry.open(_endpoint(), SimpleNamespace(result_uid="r", name="r", lineage=()))
    registry.mark_exporting(session)
    session.worker = (object(), object())
    registry.close(session)
    assert session.cancelled and session.worker is not None
    assert "r" in registry.held_result_uids()
    session.worker = None
    registry.forget(session.uid)
    assert "r" not in registry.held_result_uids()


def test_workflow_shutdown_keeps_handles_an_export_still_leases(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    raw = _h5(tmp_path / "scan.h5")
    loaded, held = [], set()
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    controller = WorkflowController(comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None),
                                    SimpleNamespace(getActiveResult=lambda: None))
    controller.addHolder(lambda: set(held))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    result = loaded.pop()[1]
    held.add(result.result_uid)                       # an export outlived the endpoint shutdown wait
    assert controller.shutdown() is True
    assert controller._retained and np.asarray(result.data).shape == (2, 8, 8)
    held.clear()
    assert controller.shutdown() is True and controller._retained == []


# 4. closing an endpoint session triggers a release check --------------------------------------------

def test_closing_a_session_asks_the_workflow_controller_to_release(registry, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    raw = _h5(tmp_path / "scan.h5")
    loaded, held, changed = [], set(), _Signal()
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    controller = WorkflowController(comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None),
                                    SimpleNamespace(getActiveResult=lambda: None))
    controller.addHolder(lambda: set(held), changed=changed)
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    held.add(loaded.pop()[1].result_uid)             # the result left the list; a session shows it
    changed.emit()
    assert controller._retained
    held.clear()                                      # the session closes
    changed.emit()
    assert controller._retained == []


def test_the_endpoint_controller_announces_session_changes(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _controller, _endpoint

    controller, _, _, _ = _controller(tmp_path, _image())
    events = []
    controller.sigSessionsChanged.connect(lambda: events.append(controller.heldResultUids()))
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    assert events and session.result_uid in events[-1]
    controller.closeSession(session.uid)
    assert session.result_uid not in events[-1]


# 5. top-level embedded provenance is validated too --------------------------------------------------

@pytest.mark.parametrize("attrs, field", [
    ({"graph": "[]"}, "graph"),
    ({"artifact": 3}, "artifact"),
    ({"processing_history": "{}"}, "processing_history"),
    ({"processing_history": "{not json"}, "processing_history"),
])
def test_top_level_embedded_provenance_with_wrong_types_raises(tmp_path, attrs, field):
    with h5py.File(str(tmp_path / "e.h5"), "w") as handle:
        for key, value in attrs.items():
            handle.attrs[key] = value
    with pytest.raises(ProvenanceReadError, match=field):
        read_provenance(tmp_path / "e.h5")


def test_a_real_saved_file_still_reads_back(tmp_path):
    result = _image()
    result.save(tmp_path / "x.h5", "hdf5")
    document = read_provenance(tmp_path / "x.h5")
    assert document.artifact and document.artifact["primary"] == "x.h5"


# 6. numpy bytes hash like bytes, not like their repr -----------------------------------------------

def test_numpy_bytes_attributes_get_the_typed_marker():
    assert attrs_digest({"b": np.bytes_(b"\xff")}) != attrs_digest({"b": "b'\\xff'"})
    assert attrs_digest({"b": np.bytes_(b"\xff")}) == attrs_digest({"b": b"\xff"})
    assert attrs_digest({"b": np.bytes_(b"\xff")}) != attrs_digest({"b": np.bytes_(b"\xfe")})


# 7. a successful save reports scaffolding it could not remove ------------------------------------

def test_a_successful_save_reports_a_backup_it_could_not_remove(tmp_path, monkeypatch):
    import imswitch.improcess.model.save_protocol as protocol

    result = _image()
    target = tmp_path / "x.ome.tif"
    result.save(target)
    real_remove = protocol._remove

    def backup_is_stuck(path):
        if ".backup-" in path.name:
            return "directory busy"
        return real_remove(path)

    monkeypatch.setattr(protocol, "_remove", backup_is_stuck)
    receipt = result.save(target, "tiff", overwrite=True)
    assert receipt.primary == target and len(receipt.leftovers) == 1
    assert "backup" in receipt.leftovers[0] and "directory busy" in receipt.leftovers[0]
    backups = [p for p in tmp_path.iterdir() if p.name.startswith(".x.ome.tif.backup-")]
    assert len(backups) == 1 and str(backups[0]) in receipt.leftovers[0]
    clean = result.save(tmp_path / "y.ome.tif")
    assert clean.leftovers == ()


# 8. metres round-trip to napari -------------------------------------------------------------------

def test_meter_units_reach_napari():
    assert layer_units("m", 2) == ("meter", "meter")


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

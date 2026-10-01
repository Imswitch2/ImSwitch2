"""Regressions for the eighth review round: each test names the finding it pins."""

import os
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
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


def _workflow_controller(loaded):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append(("x", r))),
                           getAllResults=lambda: list(loaded))
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: None)
    return WorkflowController(comm, view, SimpleNamespace(getActiveResult=lambda: None))


# 1. a completion delivered after shutdown is discarded ------------------------------------------

def test_a_completion_that_lands_after_shutdown_is_closed_not_published(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    loaded = []
    controller = _workflow_controller(loaded)
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    report = run(wf, registry=registry, out_dir=tmp_path)
    assert controller.shutdown() is True
    controller._onFinished(report)                     # the queued signal arrives late
    assert loaded == [] and controller._retained == [] and report._sources == []
    failed_report = run(wf, registry=registry, out_dir=tmp_path)
    controller._onFailed("late failure", failed_report)
    assert loaded == [] and controller._retained == [] and failed_report._sources == []


# 2. loaded results and external holders have different shutdown semantics ------------------------

def test_shutdown_closes_sources_of_loaded_results_but_keeps_external_leases(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    loaded, held = [], set()
    controller = _workflow_controller(loaded)
    controller.addHolder(lambda: set(held))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    assert loaded and controller._retained
    assert controller.shutdown() is True
    assert controller._retained == []                  # loaded-only: the list is going away with the window

    controller = _workflow_controller(loaded)
    controller.addHolder(lambda: set(held))
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    held.add(loaded[-1][1].result_uid)                 # an export thread still reads it
    assert controller.shutdown() is True
    assert controller._retained                        # external lease survives shutdown


def test_a_holder_that_cannot_answer_keeps_every_source_open_at_shutdown(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    loaded = []
    controller = _workflow_controller(loaded)

    def broken_holder():
        raise RuntimeError("endpoint registry gone")

    controller.addHolder(broken_holder)
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"])])
    controller._onFinished(run(wf, registry=registry, out_dir=tmp_path))
    loaded.clear()
    assert controller.releaseUnusedSources() == 0      # unknown is not "nobody"
    assert controller.shutdown() is True
    assert controller._retained                        # unknown at shutdown too: nothing is closed


# 3. closeAll announces the tombstones it removed --------------------------------------------------

def test_close_all_announces_the_lease_change_after_forgetting_sessions(tmp_path):
    from imswitch.improcess._test.test_napari_endpoint_controller import _controller, _endpoint

    controller, _, _, _ = _controller(tmp_path, _image())
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    seen = []
    controller.sigSessionsChanged.connect(lambda: seen.append(set(controller.heldResultUids())))
    controller.closeAll()
    assert seen and seen[-1] == set() and session.result_uid not in seen[-1]
    assert controller.registry.get(session.uid) is None


# 4. repeated shutdown parks the run once ----------------------------------------------------------

def test_repeated_shutdown_does_not_duplicate_the_orphaned_run():
    from imswitch.improcess.controller import WorkflowController as module
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    class StuckThread:
        def requestInterruption(self):
            pass

        def quit(self):
            pass

        def isRunning(self):
            return True

        def wait(self, ms):
            return False

    controller = WorkflowController(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    thread, worker = StuckThread(), SimpleNamespace(cancel=lambda: None)
    controller._thread, controller._worker = thread, worker
    before = len(module._ORPHANED_RUNS)
    assert controller.shutdown(wait_ms=1) is False
    assert controller.shutdown(wait_ms=1) is False
    assert module._ORPHANED_RUNS.count((thread, worker)) == 1
    module._ORPHANED_RUNS.remove((thread, worker))
    assert len(module._ORPHANED_RUNS) == before


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

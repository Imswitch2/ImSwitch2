"""Reloading plugins under a running directory watch.

The watch is asked about first; Yes winds it down -- the entry in hand is
finished with the frames already read, nothing new is started -- and the
reload runs once monitoring has stopped.

The DWC runs with its real workers and threads on real Zarr recordings; only
the Monitor checkbox is a stand-in (real widgets do not survive pytest here).

    pytest imswitch/improcess/_test/test_directory_watcher_wind_down.py -v
"""

import os
import time
from types import SimpleNamespace

import numpy as np
import pytest
import zarr
from qtpy import QtCore, QtWidgets

from imswitch.improcess.controller.DirectoryWatcherController import (
    DirectoryWatcherController,
)
from imswitch.improcess.controller.ImProcessMainController import (
    ImProcessMainController,
)
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
)

# Module-level: a QApplication held by a local dies when the function
# returns, and takes every QObject with it.
_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait_for(condition, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        _APP.processEvents()
        time.sleep(0.01)


def _recording(root, name, frames, *, still_writing=False):
    group = zarr.open_group(os.path.join(root, name), mode="w")
    group.create_group("Cam/metadata/Rec").attrs["Mode"] = "SpecFrames"
    data = group.create_array("Cam/data", shape=(frames, 8, 8), dtype=np.uint16, chunks=(1, 8, 8))
    data[:] = np.arange(frames, dtype=np.uint16)[:, None, None]
    data.attrs.update({
        "recording:expected_frames": frames if not still_writing else frames * 10,
        "recording:frames_committed": frames,
        "writing": still_writing,
    })


class _Check:
    """The Monitor checkbox: unchecking it is what stops the watch."""

    def __init__(self, widget):
        self._widget = widget
        self.checked = False
        self.enabled = True

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        if bool(value) != self.checked:
            self.checked = bool(value)
            self._widget.sigMonitorChanged.emit(self.checked)

    def setEnabled(self, value):
        self.enabled = bool(value)


class _FakeDWW(QtCore.QObject):
    sigMonitorChanged = QtCore.Signal(bool)

    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        self.monitorFolderCheck = _Check(self)
        self.errors = []

    def showError(self, message):
        self.errors.append(message)  # recorded, not a dialog


class _Viewer(QtCore.QObject):
    """The live signals the ProcessWorker's results reach."""

    sigLiveResultUpdated = QtCore.Signal(object)
    sigLiveTimepointUpdated = QtCore.Signal(int, object)
    sigLiveTimepointDone = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.results = []
        self.sigLiveResultUpdated.connect(self.results.append)

    def names(self):
        return {result.name for result in self.results}


@pytest.fixture
def watch(tmp_path):
    viewer = _Viewer()
    widget = _FakeDWW(tmp_path)
    main = SimpleNamespace(_activeReconstructor=ViewOnlyReconstructor())
    dwc = DirectoryWatcherController(
        viewer, widget=widget, factory=None, moduleCommChannel=None, main_controller=main,
    )
    yield dwc, widget, viewer, tmp_path
    widget.monitorFolderCheck.setChecked(False)
    # a worker left running past the stop timeout must end before the DWC
    # holding it goes: Qt aborts on a QThread destroyed while it runs
    _wait_for(lambda: all(thread.isFinished() for _, thread in dwc._stragglers))


# --- the running check the reload asks -------------------------------------

def test_a_watch_counts_as_running_from_tick_to_untick(watch):
    dwc, widget, _viewer, _root = watch
    assert not dwc.isLiveReconstructionRunning()

    widget.monitorFolderCheck.setChecked(True)
    assert dwc.isLiveReconstructionRunning()

    widget.monitorFolderCheck.setChecked(False)
    assert not dwc.isLiveReconstructionRunning()


def test_a_refused_start_tells_the_user_why(tmp_path):
    widget = _FakeDWW(tmp_path / "missing")
    dwc = DirectoryWatcherController(
        None, widget=widget, factory=None, moduleCommChannel=None,
        main_controller=SimpleNamespace(_activeReconstructor=ViewOnlyReconstructor()),
    )

    widget.monitorFolderCheck.setChecked(True)

    assert len(widget.errors) == 1 and widget.errors[0].startswith("Not a folder")
    assert not widget.monitorFolderCheck.isChecked()
    assert not dwc.isLiveReconstructionRunning()


def test_a_second_start_is_reported_not_restarted_over(watch):
    """Only a signal connected twice can start a running watch: the bug is
    logged, and the watch carries on as it was."""
    dwc, widget, _viewer, _root = watch
    errors = []
    log_error = dwc._logger.error
    dwc._logger.error = lambda message, *a, **k: (errors.append(message), log_error(message, *a, **k))
    widget.monitorFolderCheck.setChecked(True)
    workers = (dwc._observer_worker, dwc._stream_worker, dwc._process_worker)

    widget.sigMonitorChanged.emit(True)            # as a second connection would

    assert (dwc._observer_worker, dwc._stream_worker, dwc._process_worker) == workers
    assert dwc._observer_thread.isRunning()
    assert len(errors) == 1 and "connected more than once" in errors[0]


def test_only_the_process_worker_holds_the_reconstructor_instance(watch):
    dwc, widget, _viewer, _root = watch
    reconstructor = dwc._main_controller._activeReconstructor
    widget.monitorFolderCheck.setChecked(True)

    assert dwc._stream_worker._reconstructor_class is type(reconstructor)
    assert dwc._process_worker._reconstructor is reconstructor


def test_a_contract_override_must_be_a_classmethod():
    from imswitch.improcess.reconstructors.base import Contract

    with pytest.raises(TypeError, match="must be a @classmethod"):
        class _Instance(ViewOnlyReconstructor):
            def contract(self, info, params):
                return Contract(0, 1, dict(params))

    class _Class(ViewOnlyReconstructor):
        @classmethod
        def contract(cls, info, params):
            return Contract(2, 4, dict(params))

    assert _Class.contract(None, {}).init_frames == 2


# --- winding down ----------------------------------------------------------

def test_winding_down_an_idle_watch_stops_it_and_calls_back(watch):
    dwc, widget, _viewer, _root = watch
    widget.monitorFolderCheck.setChecked(True)
    called = []

    dwc.stopAfterCurrent(lambda: called.append(dwc.isLiveReconstructionRunning()))
    _wait_for(lambda: called)

    assert called == [False]                      # called once, after the stop
    assert not widget.monitorFolderCheck.isChecked()
    assert widget.monitorFolderCheck.enabled


def test_winding_down_without_a_watch_calls_back_at_once(watch):
    dwc, _widget, _viewer, _root = watch
    called = []

    dwc.stopAfterCurrent(lambda: called.append(True))

    assert called == [True]


def test_the_entry_in_hand_is_finished_and_the_next_is_never_started(watch):
    dwc, widget, viewer, root = watch
    # a recording that never finishes on its own, and one queued behind it
    _recording(root, "a_live.zarr", 6, still_writing=True)
    _recording(root, "b_next.zarr", 4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a_live" in viewer.names())  # streaming has begun
    called = []

    dwc.stopAfterCurrent(lambda: called.append(True))
    assert not widget.monitorFolderCheck.enabled   # no toggling meanwhile
    _wait_for(lambda: called)

    final = viewer.results[-1]
    assert final.name == "a_live"                  # its result, with what was read
    assert np.array_equal(final.data.reshape(-1, 8, 8)[:6, 0, 0], np.arange(6))
    assert "b_next" not in viewer.names()          # never started
    assert not dwc.isLiveReconstructionRunning()
    assert widget.monitorFolderCheck.enabled


def test_a_hard_stop_during_the_wind_down_still_calls_back_once(watch):
    dwc, widget, _viewer, root = watch
    _recording(root, "a_live.zarr", 6, still_writing=True)
    widget.monitorFolderCheck.setChecked(True)
    called = []

    dwc.stopAfterCurrent(lambda: called.append(True))
    widget.monitorFolderCheck.setChecked(False)    # e.g. a reconstructor switch
    _APP.processEvents()

    assert called == [True]
    assert widget.monitorFolderCheck.enabled


# --- closing ImProcess -----------------------------------------------------

def test_closing_stops_the_watch_before_the_shutdown_barrier_moves_on(tmp_path):
    """Through the factory's own shutdown path, as ImProcess closes."""
    from imswitch.improcess.controller.basecontrollers import (
        ImProcessWidgetControllerFactory,
    )

    _recording(tmp_path, "a_live.zarr", 6, still_writing=True)
    factory = ImProcessWidgetControllerFactory(_Viewer(), None)
    widget = _FakeDWW(tmp_path)
    dwc = factory.createController(
        DirectoryWatcherController, widget,
        main_controller=SimpleNamespace(_activeReconstructor=ViewOnlyReconstructor()),
    )
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: dwc._process_worker._session is not None)  # mid-entry
    threads = (dwc._observer_thread, dwc._stream_thread, dwc._process_thread)
    process_worker = dwc._process_worker

    assert factory.closeAllCreatedControllers(waitTimeoutS=5) is True

    assert not dwc.isLiveReconstructionRunning()
    assert all(thread.isFinished() for thread in threads)
    assert process_worker._session is None         # closed, not left on the device


def test_closing_during_a_wind_down_does_not_run_what_it_was_for(watch):
    dwc, widget, _viewer, root = watch
    _recording(root, "a_live.zarr", 6, still_writing=True)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: dwc._stream_worker_busy)     # an entry in hand: the wind-down waits
    reloaded = []
    dwc.stopAfterCurrent(lambda: reloaded.append(True))

    dwc.closeEvent()
    _APP.processEvents()

    assert reloaded == []                          # no plugin reload during shutdown
    assert not dwc.isLiveReconstructionRunning()


def test_closing_without_a_watch_is_harmless(watch):
    dwc, _widget, _viewer, _root = watch

    dwc.closeEvent()

    assert not dwc.isLiveReconstructionRunning()


# --- stopping takes bounded time, except for the chunk in hand -------------

def test_a_worker_stuck_in_a_read_does_not_hold_up_the_stop(watch, monkeypatch, tmp_path):
    """A folder listing that hangs (a slow share) is left to end on its own,
    and what it finds afterwards never reaches the next watch."""
    import threading

    import imswitch.improcess.controller.DirectoryWatcherController as module

    dwc, widget, viewer, root = watch
    monkeypatch.setattr(DirectoryWatcherController, "STOP_TIMEOUT_S", 0.3)
    real_listdir = os.listdir
    hang, hanging = threading.Event(), threading.Event()

    def listdir(path):
        if hang.is_set() and os.path.samefile(path, root):
            hanging.set()
            time.sleep(1.5)  # the share answers late, with what is there by then
        return real_listdir(path)

    monkeypatch.setattr(module.os, "listdir", listdir)
    widget.monitorFolderCheck.setChecked(True)
    hang.set()
    _wait_for(hanging.is_set)

    started = time.monotonic()
    widget.monitorFolderCheck.setChecked(False)
    took = time.monotonic() - started

    assert took < 1.2                              # did not wait out the listing
    (straggler, thread), = dwc._stragglers
    assert type(straggler).__name__ == "ObserverWorker"

    # a new watch elsewhere, while the old listing still hangs
    other = tmp_path / "other"
    other.mkdir()
    widget.path = str(other)
    widget.monitorFolderCheck.setChecked(True)
    _recording(root, "late.zarr", 2)               # the late listing will see it
    _wait_for(thread.isFinished)
    for _ in range(50):
        _APP.processEvents()
        time.sleep(0.01)

    assert "late" not in viewer.names()            # the old watch's find, dropped
    assert dwc._entry_queue == []


def test_the_chunk_being_processed_is_finished_before_the_stop(watch, monkeypatch):
    import threading

    from imswitch.improcess.reconstructors.view_only.live_session import (
        ViewOnlyLiveSession,
    )

    dwc, widget, _viewer, root = watch
    monkeypatch.setattr(DirectoryWatcherController, "STOP_TIMEOUT_S", 0.2)
    in_chunk, chunks_done = threading.Event(), []
    real_push = ViewOnlyLiveSession.push

    def slow_push(self, frames, start, end):
        if start == 0:                             # begin() pushing its init frame
            return real_push(self, frames, start, end)
        in_chunk.set()
        time.sleep(1.0)                            # longer than the timeout
        real_push(self, frames, start, end)
        chunks_done.append((start, end))

    monkeypatch.setattr(ViewOnlyLiveSession, "push", slow_push)
    _recording(root, "a_live.zarr", 6, still_writing=True)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(in_chunk.is_set)

    started = time.monotonic()
    widget.monitorFolderCheck.setChecked(False)
    took = time.monotonic() - started

    assert len(chunks_done) == 1                   # the chunk in hand, finished
    assert took > 0.5                              # waited past the timeout for it
    assert not dwc.isLiveReconstructionRunning()


# --- the reload's gate -----------------------------------------------------

def _controller(*, running, answer):
    asked, reloaded, wound_down = [], [], []
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = SimpleNamespace(
        confirmDirectoryWatcherInterruption=lambda message: (asked.append(message), answer)[1]
    )
    controller.mainViewController = SimpleNamespace(
        directoryWatcherController=SimpleNamespace(
            isLiveReconstructionRunning=lambda: running,
            stopAfterCurrent=wound_down.append,
        )
    )
    controller._reload_user_plugins_now = lambda: reloaded.append(True)
    return controller, asked, reloaded, wound_down


def test_without_a_watch_the_reload_runs_at_once_unasked():
    controller, asked, reloaded, wound_down = _controller(running=False, answer=False)

    controller._reload_user_plugins()

    assert (asked, reloaded, wound_down) == ([], [True], [])


def test_declining_leaves_everything_as_it_is():
    controller, asked, reloaded, wound_down = _controller(running=True, answer=False)

    controller._reload_user_plugins()

    assert len(asked) == 1
    assert (reloaded, wound_down) == ([], [])


def test_accepting_winds_the_watch_down_and_reloads_when_it_has_stopped():
    controller, asked, reloaded, wound_down = _controller(running=True, answer=True)

    controller._reload_user_plugins()

    assert "Reloading plugins" in asked[0]
    assert reloaded == []                          # not yet: the watch is winding down
    wound_down[0]()                                # the watch has stopped
    assert reloaded == [True]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


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

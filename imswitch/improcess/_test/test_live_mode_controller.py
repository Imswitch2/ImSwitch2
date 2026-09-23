"""Tests for LiveModeController: DirectoryWatcher + JobQueue integration.

The controller is built with ``__new__`` (no Qt widget) and a fake
``LiveReconstructionController`` that just records ``start`` calls.
"""

import collections
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qtpy import QtCore

from imswitch.improcess.controller.LiveModeController import LiveModeController
from imswitch.improcess.live.discovery import JobQueue
from imswitch.improcess.live.source_type import (
    LAYOUT_MULTIFILE_LAPSE,
    LAYOUT_SINGLE,
)


class _FakeLive(QtCore.QObject):
    sigFinished = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.start_calls = []
        self.start_names = []
        self.start_return = True
        self.stops = 0
        self.stop_kwargs = []

    def start(self, reconstructor, source, params, source_arg=None, name=None):
        self.start_calls.append(source_arg)
        self.start_names.append(name)
        return self.start_return

    def stop(self, *, graceful=True, notify_finished=False):
        self.stops += 1
        self.stop_kwargs.append({'graceful': graceful,
                                 'notify_finished': notify_finished})
        if notify_finished:
            self.sigFinished.emit()


class _FakeCheck:
    def __init__(self, checked=True):
        self.checked = checked

    def setChecked(self, value):
        self.checked = bool(value)

    def isChecked(self):
        return self.checked


class _FakeWidget:
    """The slice of DirectoryWatcherFrame that LiveModeController drives."""

    def __init__(self, path=""):
        self.path = path
        self.liveCheck = _FakeCheck()
        self.saveCheck = _FakeCheck(checked=False)


def _controller(reconstructor=None):
    c = LiveModeController.__new__(LiveModeController)
    c._logger = MagicMock()
    # _activeReconstructor / _widget stay camelCase: they belong to
    # ImProcessMainViewController, not to LiveModeController.
    c._main_controller = SimpleNamespace(
        _activeReconstructor=reconstructor or object(), _widget=None
    )
    c._run_controller = _FakeLive()
    c._job_queue = JobQueue()
    c._directory_watcher = None
    c._tick_timer = None
    c._currently_processing = False
    c._watched_folder = None
    c._current_job_name = None
    c._current_save_stem = None
    c._job_name_counts = collections.Counter()
    c._widget = _FakeWidget()
    c._commChannel = MagicMock()
    # _start_live() wires this in the real controller; the stub is built with
    # __new__ so it has to be done here or sigFinished goes nowhere. A lambda,
    # not the bound method: c has no initialised QObject to receive on.
    c._run_controller.sigFinished.connect(lambda: c._on_job_finished())
    return c


def _store(folder, name="rec_scan0.zarr", *, num_timepoints=3, frames=4):
    """A real Zarr timepoint store.

    An empty directory used to be enough. The controller now probes each
    candidate for what its raw data *is* -- to pick a reader and to ask the
    reconstructor whether it can use it -- so a fake store reads as "not
    readable yet" and is deferred rather than started.
    """
    import zarr

    path = os.path.join(str(folder), name)
    group = zarr.open_group(path, mode="w")
    array = group.create_array("Cam", shape=(frames, 8, 8), dtype="uint16")
    array.attrs["writing"] = False
    array.attrs["recording:detector_name"] = "Cam"
    array.attrs["recording:frames_per_stack"] = frames
    array.attrs["recording:num_timepoints"] = int(num_timepoints)
    array.attrs["recording:single_lapse_file"] = False
    return path


# --------------------------------------------------------------------------

def test_timelapse_with_a_store_is_queued_and_started(tmp_path):
    folder = tmp_path / "lapseA"
    seed = _store(folder)

    c = _controller()
    c._on_timelapse_found(str(folder))

    assert c._run_controller.start_calls == [seed]
    assert c._currently_processing is True


def test_folder_without_a_store_stays_pending_until_one_appears(tmp_path):
    folder = tmp_path / "lapseA"
    folder.mkdir()

    c = _controller()
    c._on_timelapse_found(str(folder))
    assert c._run_controller.start_calls == []
    assert len(c._job_queue) == 1          # still pending

    seed = _store(folder)
    c._process_next_job()                  # tick timer would call this
    assert c._run_controller.start_calls == [seed]


def test_one_run_at_a_time_advances_on_finish(tmp_path):
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_timelapse_found(str(tmp_path / "b"))
    assert len(c._run_controller.start_calls) == 1   # b waits

    c._on_job_finished()
    assert len(c._run_controller.start_calls) == 2


def test_start_failure_does_not_stall_the_queue(tmp_path):
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._run_controller.start_return = False
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_timelapse_found(str(tmp_path / "b"))

    assert len(c._run_controller.start_calls) == 2
    assert c._currently_processing is False


def test_job_is_named_after_its_timelapse_folder(tmp_path):
    """Each job carries its folder name, so it gets its own viewer entry.

    ``LiveReconstructionController`` puts this name on every result the run
    produces, and ``ReconstructionViewController`` opens a new data object
    whenever that name changes -- so without it, every timelapse would stream
    into whichever entry happened to be selected.
    """
    _store(tmp_path / "timelapse_00")
    _store(tmp_path / "timelapse_01")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "timelapse_00"))
    c._on_job_finished()
    c._on_timelapse_found(str(tmp_path / "timelapse_01"))

    assert c._run_controller.start_names == ["timelapse_00", "timelapse_01"]


def test_job_name_survives_a_trailing_separator(tmp_path):
    """A folder path with a trailing separator still yields the folder name."""
    folder = tmp_path / "timelapse_09"
    _store(folder)

    c = _controller()
    c._on_timelapse_found(str(folder) + os.sep)

    assert c._run_controller.start_names == ["timelapse_09"]


def test_seed_is_the_lowest_indexed_store_via_multifile_source(tmp_path):
    """The seed is timepoint 0, and the folder classifies as a multi-file lapse
    -- which is what selects the reader that follows the siblings."""
    folder = tmp_path / "lapseA"
    _store(folder, "rec_scan__02__CAM.zarr")
    seed = _store(folder, "rec_scan__00__CAM.zarr")
    _store(folder, "rec_scan__01__CAM.zarr")

    c = _controller()
    with patch(
        "imswitch.improcess.controller.LiveModeController.make_live_source"
    ) as mock_factory:
        c._on_timelapse_found(str(folder))

    mock_factory.assert_called_once()
    assert mock_factory.call_args.args[0] == seed
    assert mock_factory.call_args.args[1].layout == LAYOUT_MULTIFILE_LAPSE
    assert c._run_controller.start_calls == [seed]


def _h5_dataset(folder, name="solo.h5", *, frames=6):
    """A single-file HDF5 recording sitting directly in the watched root."""
    import h5py
    import numpy as np

    os.makedirs(str(folder), exist_ok=True)
    path = os.path.join(str(folder), name)
    with h5py.File(path, "w", libver="latest") as handle:
        dataset = handle.create_dataset(
            "Cam", data=np.zeros((frames, 8, 8), "uint16")
        )
        dataset.attrs["recording:detector_name"] = "Cam"
    return path


def test_a_single_dataset_job_is_started_too(tmp_path):
    """The watcher is no longer folders-only: a lone recording in the root is a
    job, and it classifies as a single dataset rather than a lapse -- which is
    what picks the single-store reader instead of the sibling-following one."""
    seed = _h5_dataset(tmp_path)

    c = _controller()
    with patch(
        "imswitch.improcess.controller.LiveModeController.make_live_source"
    ) as mock_factory:
        c._on_timelapse_found(seed)

    mock_factory.assert_called_once()
    assert mock_factory.call_args.args[1].layout == LAYOUT_SINGLE
    assert c._run_controller.start_calls == [seed]


def test_a_job_the_reconstructor_cannot_use_is_skipped(tmp_path):
    """A camera recording -- one frame per timepoint -- has nothing for a
    scanning reconstructor to reassemble, so it never starts."""
    class _NeedsStacks:
        name = "Scanning"

        @staticmethod
        def accepts_raw_source(source_type):
            return source_type.has_frame_stacks

    folder = tmp_path / "camera"
    _store(folder, "rec_scan0.zarr", num_timepoints=5, frames=1)

    c = _controller(reconstructor=_NeedsStacks())
    c._on_timelapse_found(str(folder))

    assert c._run_controller.start_calls == []
    assert len(c._job_queue) == 0            # skipped, not left pending


# --- reset --------------------------------------------------------------
#
# Reset forgets which folders have been processed so the same root can be
# reconstructed again. Re-runs must land in their OWN data objects: the viewer
# keys an entry on the run's name, so reusing a name would write into the
# earlier run's object instead of creating a new one.


def test_reset_clears_the_watcher_seen_set_and_the_queue(tmp_path):
    from imswitch.improcess.live.discovery import DirectoryWatcher

    _store(tmp_path / "timelapse_00")
    c = _controller()
    c._directory_watcher = DirectoryWatcher(str(tmp_path))
    c._watched_folder = str(tmp_path)

    assert c._directory_watcher.scan_once() == [
        os.path.abspath(str(tmp_path / "timelapse_00"))
    ]
    assert c._directory_watcher.scan_once() == []     # already seen

    c._job_queue.add(str(tmp_path / "pending"))
    c._on_reset_clicked()

    assert len(c._job_queue) == 0                     # queue dropped
    assert c._directory_watcher.scan_once() == [      # re-discovered
        os.path.abspath(str(tmp_path / "timelapse_00"))
    ]


def test_rerun_after_reset_gets_its_own_indexed_name(tmp_path):
    folder = tmp_path / "timelapse_00"
    _store(folder)

    c = _controller()
    c._on_timelapse_found(str(folder))
    c._on_job_finished()

    c._on_reset_clicked()
    c._on_timelapse_found(str(folder))
    c._on_job_finished()

    c._on_reset_clicked()
    c._on_timelapse_found(str(folder))

    assert c._run_controller.start_names == [
        "timelapse_00", "timelapse_00.1", "timelapse_00.2"
    ]


def test_first_run_of_a_folder_keeps_its_bare_name(tmp_path):
    """Only duplicates are indexed -- the common case stays clean."""
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_job_finished()
    c._on_timelapse_found(str(tmp_path / "b"))

    assert c._run_controller.start_names == ["a", "b"]


def test_reset_shuts_live_mode_down(tmp_path):
    """Reset is a full stop -- run ended, watcher/tick stopped, box unchecked.

    LiveReconstructionController.stop() does not emit sigFinished, so the
    one-run-at-a-time slot has to be released by _stop_live or the queue would
    stay wedged after a restart.
    """
    _store(tmp_path / "timelapse_00")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "timelapse_00"))
    assert c._currently_processing is True

    c._on_reset_clicked()

    assert c._run_controller.stops == 1          # run in flight ended
    assert c._currently_processing is False      # slot freed
    assert c._widget.liveCheck.isChecked() is False   # UI reflects the stop


def test_reset_leaves_nothing_queued_or_running(tmp_path):
    """Nothing may keep processing once live mode is off."""
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_timelapse_found(str(tmp_path / "b"))    # b queued behind a
    assert len(c._job_queue) == 1

    c._on_reset_clicked()

    assert len(c._job_queue) == 0
    assert c._currently_processing is False


def test_reset_is_safe_with_nothing_running():
    c = _controller()
    c._on_reset_clicked()          # must not raise
    assert c._currently_processing is False
    assert c._widget.liveCheck.isChecked() is False


# --- skip directory ------------------------------------------------------
#
# Skip ends only the current run; live mode keeps watching and the queue moves
# on. What the run reconstructed stays in the viewer, and the timepoint in
# progress is finished first (stop() is graceful by default).


def test_skip_ends_the_current_run_and_starts_the_next(tmp_path):
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_timelapse_found(str(tmp_path / "b"))      # queued behind a
    assert c._run_controller.start_names == ["a"]

    c._on_skip_clicked()

    assert c._run_controller.stops == 1
    assert c._run_controller.start_names == ["a", "b"]


def test_skip_asks_the_queue_to_advance_and_keeps_the_timepoint(tmp_path):
    """notify_finished is what frees the slot, and only once teardown is done.

    Without it a graceful stop would return while the run is still draining,
    the next start() would force-kill it, and the timepoint being preserved
    would be lost anyway.
    """
    _store(tmp_path / "a")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_skip_clicked()

    assert c._run_controller.stop_kwargs == [
        {"graceful": True, "notify_finished": True}
    ]


def test_skip_does_not_stop_watching(tmp_path):
    """Unlike Reset, live mode stays on and later discoveries still run."""
    _store(tmp_path / "a")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_skip_clicked()                            # abandoned, queue now empty

    _store(tmp_path / "c")
    c._on_timelapse_found(str(tmp_path / "c"))      # discovered after the skip

    assert c._widget.liveCheck.isChecked() is True
    assert c._run_controller.start_names == ["a", "c"]


def test_skipped_folder_is_not_reprocessed(tmp_path):
    """Skip means give up on it -- it must not come back around."""
    _store(tmp_path / "a")
    _store(tmp_path / "b")

    c = _controller()
    c._on_timelapse_found(str(tmp_path / "a"))
    c._on_timelapse_found(str(tmp_path / "b"))
    c._on_skip_clicked()                            # -> b
    c._on_job_finished()                            # b done, queue empty

    assert c._run_controller.start_names == ["a", "b"]
    assert len(c._job_queue) == 0


def test_skip_with_nothing_running_is_a_no_op():
    c = _controller()
    c._on_skip_clicked()

    assert c._run_controller.stops == 0


# --- saving a finished timelapse -----------------------------------------
#
# When a timelapse finishes or is skipped and the save toggle is on, its data
# object is written beside the watched folder:
#     <parent>/<root>/timelapse_00  ->  <parent>/<root>_recon/timelapse_00_recon.tif
# The viewer owns the buffer, so the controller only decides where.


def _save_requests(c):
    return [call.args for call in c._commChannel.sigSaveLiveResult.emit.call_args_list]


def test_no_save_request_when_the_toggle_is_off(tmp_path):
    _store(tmp_path / "timelapse_00")
    c = _controller()
    c._watched_folder = str(tmp_path)

    c._on_timelapse_found(str(tmp_path / "timelapse_00"))
    c._on_job_finished()

    assert _save_requests(c) == []


def test_finished_timelapse_is_saved_beside_the_watched_folder(tmp_path):
    root = tmp_path / "root"
    (root).mkdir()
    _store(root / "timelapse_00")

    c = _controller()
    c._watched_folder = str(root)
    c._widget.saveCheck.setChecked(True)

    c._on_timelapse_found(str(root / "timelapse_00"))
    c._on_job_finished()

    expected = os.path.join(str(tmp_path / "root_recon"), "timelapse_00_recon.tif")
    assert _save_requests(c) == [("timelapse_00", expected)]
    assert (tmp_path / "root_recon").is_dir()      # created, and OUTSIDE root
    assert not (root / "root_recon").exists()      # never nested inside


def test_skipped_timelapse_is_saved_too(tmp_path):
    """Skip keeps what was reconstructed, so it is worth writing out."""
    root = tmp_path / "root"
    root.mkdir()
    _store(root / "timelapse_00")

    c = _controller()
    c._watched_folder = str(root)
    c._widget.saveCheck.setChecked(True)

    c._on_timelapse_found(str(root / "timelapse_00"))
    c._on_skip_clicked()                            # -> sigFinished -> save

    assert [name for name, _ in _save_requests(c)] == ["timelapse_00"]


def test_each_run_is_saved_under_its_own_name(tmp_path):
    """A re-run after Reset is a separate object, so a separate file."""
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "timelapse_00"
    _store(folder)

    c = _controller()
    c._watched_folder = str(root)
    c._widget.saveCheck.setChecked(True)

    c._on_timelapse_found(str(folder))
    c._on_job_finished()
    c._job_queue.add(str(folder))                   # as a reset re-discovery would
    c._process_next_job()
    c._on_job_finished()

    # The entry keeps ".1"; the file puts the index last, after "_recon".
    assert [name for name, _ in _save_requests(c)] == [
        "timelapse_00", "timelapse_00.1"
    ]
    assert [os.path.basename(path) for _, path in _save_requests(c)] == [
        "timelapse_00_recon.tif", "timelapse_00_recon_1.tif"
    ]


def test_no_save_request_for_a_run_that_never_started(tmp_path):
    """A failed startup still emits sigFinished; there is nothing to write."""
    c = _controller()
    c._watched_folder = str(tmp_path)
    c._widget.saveCheck.setChecked(True)

    c._on_job_finished()                            # no job was in flight

    assert _save_requests(c) == []


if __name__ == "__main__":
    import sys

    import pytest

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

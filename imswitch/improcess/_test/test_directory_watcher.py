"""Standalone tests for ``imswitch.improcess.live.discovery.DirectoryWatcher``.

The filesystem logic lives in ``scan_once()`` and needs no Qt event loop. The
signal / lifecycle tests need a ``QApplication`` (created here on demand) but
still no running loop -- ``_tick`` is called directly.

    pytest imswitch/improcess/_test/test_directory_watcher.py -v
"""

import os

import pytest

from imswitch.improcess.live.discovery import DirectoryWatcher


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _mk(root, *names):
    for name in names:
        os.makedirs(os.path.join(str(root), name), exist_ok=True)


# --- scan_once: the filesystem logic ---------------------------------------

def test_reports_new_dir_as_absolute_path(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path))
    assert w.scan_once() == [str(tmp_path / "measA")]


def test_dedups_already_seen_dirs(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path))
    assert w.scan_once() == [str(tmp_path / "measA")]
    assert w.scan_once() == []


def test_picks_up_a_dir_created_later(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path))
    w.scan_once()
    _mk(tmp_path, "measB")
    assert w.scan_once() == [str(tmp_path / "measB")]


def test_result_is_sorted(tmp_path):
    _mk(tmp_path, "c", "a", "b")
    w = DirectoryWatcher(str(tmp_path))
    assert w.scan_once() == [str(tmp_path / n) for n in ("a", "b", "c")]


def test_no_directory_is_excluded_by_name(tmp_path):
    """Nothing is filtered by convention: an output or cache folder simply
    never resolves into a job, and one that does is checked against the
    reconstructor before it starts. A name list could never be exhaustive."""
    _mk(tmp_path, "measA", "rec", "deskew", "__pycache__")

    w = DirectoryWatcher(str(tmp_path))

    assert w.scan_once() == [
        str(tmp_path / "__pycache__"),
        str(tmp_path / "deskew"),
        str(tmp_path / "measA"),
        str(tmp_path / "rec"),
    ]


def test_a_folder_holding_no_dataset_never_becomes_a_job(tmp_path):
    """The downstream half of the same point -- reported by the watcher, but
    the queue never hands it out."""
    from imswitch.improcess.live.discovery import JobQueue

    _mk(tmp_path, "__pycache__")
    queue = JobQueue()
    queue.add(str(tmp_path / "__pycache__"))

    assert queue.next_ready() is None
    assert len(queue) == 1          # stays pending, harmlessly


def test_files_are_ignored(tmp_path):
    _mk(tmp_path, "measA")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "data.zarr").write_text("x")  # a file, not a dir
    w = DirectoryWatcher(str(tmp_path))
    assert w.scan_once() == [str(tmp_path / "measA")]


def test_unreadable_root_returns_empty(tmp_path):
    w = DirectoryWatcher(str(tmp_path / "does_not_exist"))
    assert w.scan_once() == []



def test_single_file_datasets_are_reported(tmp_path):
    """A lone recording in the root is a job too, not only folders."""
    (tmp_path / "measurement.h5").write_bytes(b"")
    (tmp_path / "other.hdf5").write_bytes(b"")

    w = DirectoryWatcher(str(tmp_path))

    assert w.scan_once() == [
        str(tmp_path / "measurement.h5"),
        str(tmp_path / "other.hdf5"),
    ]


def test_a_zarr_store_in_the_root_is_reported(tmp_path):
    """It is a directory, so the watcher reports it; whether it is a store or a
    container of stores is JobQueue's call."""
    (tmp_path / "data.zarr").mkdir()

    w = DirectoryWatcher(str(tmp_path))

    assert w.scan_once() == [str(tmp_path / "data.zarr")]


# --- _tick + lifecycle ----------------------------------------------------

def test_tick_emits_signal_per_new_dir(tmp_path):
    _mk(tmp_path, "measA", "measB")
    w = DirectoryWatcher(str(tmp_path))
    got = []
    w.sigEntryFound.connect(got.append)
    w._tick()
    assert got == [str(tmp_path / "measA"), str(tmp_path / "measB")]
    w._tick()
    assert got == [str(tmp_path / "measA"), str(tmp_path / "measB")]  # no repeats


def test_start_scans_immediately_and_activates_timer(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path), poll_interval_ms=50)
    got = []
    w.sigEntryFound.connect(got.append)

    w.start()
    assert got == [str(tmp_path / "measA")]
    assert w.running is True

    w.stop()


def test_stop_then_start_does_not_reemit(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path), poll_interval_ms=50)
    got = []
    w.sigEntryFound.connect(got.append)

    w.start()
    assert got == [str(tmp_path / "measA")]
    w.stop()
    assert w.running is False

    w.start()                                  # seen-set persists across stop/start
    assert got == [str(tmp_path / "measA")]     # not re-emitted
    w.stop()


def test_reset_makes_next_scan_reemit(tmp_path):
    _mk(tmp_path, "measA")
    w = DirectoryWatcher(str(tmp_path))
    got = []
    w.sigEntryFound.connect(got.append)

    w._tick()
    assert got == [str(tmp_path / "measA")]
    w.reset()
    w._tick()
    assert got == [str(tmp_path / "measA"), str(tmp_path / "measA")]


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

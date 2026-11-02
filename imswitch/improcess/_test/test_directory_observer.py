"""DirectoryObserver (DO), and the DWC driving it.

``check()`` is called directly rather than waiting on the timer, so nothing
here depends on timing.

    pytest imswitch/improcess/_test/test_directory_observer.py -v
"""

import os

import pytest
from qtpy import QtCore, QtWidgets

from imswitch.improcess.controller.DirectoryWatcherController import (
    DirectoryWatcherController,
)
from imswitch.improcess.live.directory_observer import DirectoryObserver


# Module-level: a QApplication held by a local dies when the function
# returns, and takes every QObject with it.
_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _observer(root):
    """A DO and the list its emits land in."""
    do = DirectoryObserver(str(root))
    found = []
    do.sigEntryFound.connect(found.append)
    return do, found


def _names(paths):
    return [os.path.basename(p) for p in paths]


# --- what gets emitted ------------------------------------------------------

def test_entries_already_there_are_emitted_on_start(tmp_path):
    (tmp_path / "b.h5").touch()
    (tmp_path / "a_lapse").mkdir()
    do, found = _observer(tmp_path)

    do.start()
    do.stop()

    assert _names(found) == ["a_lapse", "b.h5"]


def test_files_and_folders_both_count(tmp_path):
    (tmp_path / "rec.zarr").mkdir()
    (tmp_path / "notes.txt").touch()
    do, found = _observer(tmp_path)

    do.check()

    assert sorted(_names(found)) == ["notes.txt", "rec.zarr"]


def test_paths_are_absolute(tmp_path, monkeypatch):
    (tmp_path / "x").touch()
    monkeypatch.chdir(tmp_path.parent)
    do, found = _observer(tmp_path.name)          # relative root

    do.check()

    assert os.path.isabs(found[0])
    assert found[0] == os.path.join(str(tmp_path), "x")


def test_several_new_entries_come_in_name_order(tmp_path):
    for name in ("t2", "t0", "t1"):
        (tmp_path / name).mkdir()
    do, found = _observer(tmp_path)

    do.check()

    assert _names(found) == ["t0", "t1", "t2"]


def test_an_empty_root_emits_nothing(tmp_path):
    do, found = _observer(tmp_path)

    do.check()

    assert found == []


# --- once, and only what is new --------------------------------------------

def test_a_new_entry_is_emitted_on_the_next_check(tmp_path):
    do, found = _observer(tmp_path)
    do.check()

    (tmp_path / "late").mkdir()
    do.check()

    assert _names(found) == ["late"]


def test_an_entry_is_emitted_only_once(tmp_path):
    (tmp_path / "a").mkdir()
    do, found = _observer(tmp_path)

    do.check()
    do.check()
    do.check()

    assert _names(found) == ["a"]


def test_it_never_looks_inside_an_entry(tmp_path):
    folder = tmp_path / "lapse"
    folder.mkdir()
    do, found = _observer(tmp_path)
    do.check()

    (folder / "t0.h5").touch()
    (folder / "t1.h5").touch()
    do.check()

    assert _names(found) == ["lapse"]


def test_a_deletion_is_ignored(tmp_path):
    (tmp_path / "a").mkdir()
    do, found = _observer(tmp_path)
    do.check()

    (tmp_path / "a").rmdir()
    do.check()

    assert _names(found) == ["a"]


def test_a_renamed_entry_counts_as_new(tmp_path):
    (tmp_path / "old").mkdir()
    do, found = _observer(tmp_path)
    do.check()

    (tmp_path / "old").rename(tmp_path / "new")
    do.check()

    assert _names(found) == ["old", "new"]


# --- stop / start -----------------------------------------------------------

def test_stop_then_start_emits_only_what_is_new(tmp_path):
    (tmp_path / "a").mkdir()
    do, found = _observer(tmp_path)
    do.start()
    do.stop()

    (tmp_path / "b").mkdir()
    do.start()
    do.stop()

    assert _names(found) == ["a", "b"]


def test_start_and_stop_drive_the_timer(tmp_path):
    do, _ = _observer(tmp_path)
    assert not do.running

    do.start()
    assert do.running

    do.stop()
    assert not do.running


# --- an unreadable root -----------------------------------------------------

def test_an_unreadable_root_does_not_raise(tmp_path):
    do, found = _observer(tmp_path / "missing")

    do.check()

    assert found == []


def test_it_warns_once_per_outage_not_once_per_check(tmp_path):
    do, _ = _observer(tmp_path / "missing")
    warnings = []
    do._logger = type("L", (), {
        "warning": lambda self, m: warnings.append(m),
        "info": lambda self, m: None,
    })()

    for _ in range(5):
        do.check()

    assert len(warnings) == 1


def test_it_picks_up_once_the_root_is_readable_again(tmp_path):
    root = tmp_path / "share"
    do, found = _observer(root)
    do.check()                                    # not there yet

    root.mkdir()
    (root / "a").mkdir()
    do.check()

    assert _names(found) == ["a"]


# --- the DWC ----------------------------------------------------------------

class _Check:
    def __init__(self):
        self.checked = True

    def setChecked(self, value):
        self.checked = bool(value)


class _FakeDWW(QtCore.QObject):
    """The slice of the DWW the DWC drives."""

    sigMonitorChanged = QtCore.Signal(bool)
    sigSkipEntryClicked = QtCore.Signal()
    sigResetClicked = QtCore.Signal()

    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        self.monitorFolderCheck = _Check()
        self.errors = []

    def showError(self, message):
        self.errors.append(message)  # recorded, not a dialog


def _dwc(path):
    widget = _FakeDWW(path)
    dwc = DirectoryWatcherController(
        None, widget=widget, factory=None, moduleCommChannel=None,
    )
    return dwc, widget


def test_ticking_monitor_starts_a_do_on_the_root(tmp_path):
    dwc, widget = _dwc(tmp_path)

    widget.sigMonitorChanged.emit(True)

    assert dwc._observer is not None
    assert dwc._observer.root == os.path.abspath(str(tmp_path))
    assert dwc._observer.running


def test_unticking_stops_it(tmp_path):
    dwc, widget = _dwc(tmp_path)
    widget.sigMonitorChanged.emit(True)

    widget.sigMonitorChanged.emit(False)

    assert not dwc._observer.running


def test_a_path_that_is_not_a_folder_unticks_the_box(tmp_path):
    dwc, widget = _dwc(tmp_path / "missing")

    widget.sigMonitorChanged.emit(True)

    assert dwc._observer is None
    assert widget.monitorFolderCheck.checked is False


def test_the_same_root_keeps_its_do_and_its_memory(tmp_path):
    (tmp_path / "a").mkdir()
    dwc, widget = _dwc(tmp_path)
    widget.sigMonitorChanged.emit(True)
    first = dwc._observer
    widget.sigMonitorChanged.emit(False)

    found = []
    first.sigEntryFound.connect(found.append)
    (tmp_path / "b").mkdir()
    widget.sigMonitorChanged.emit(True)

    assert dwc._observer is first
    assert _names(found) == ["b"]                 # "a" is not emitted again


def test_a_new_root_gets_a_new_do(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    dwc, widget = _dwc(one)
    widget.sigMonitorChanged.emit(True)
    first = dwc._observer
    widget.sigMonitorChanged.emit(False)

    widget.path = str(two)
    widget.sigMonitorChanged.emit(True)

    assert dwc._observer is not first
    assert dwc._observer.root == os.path.abspath(str(two))


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

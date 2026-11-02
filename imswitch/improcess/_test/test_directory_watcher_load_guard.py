"""Loading data while a directory watch is running.

Every way the user brings data into ImProcess asks first; Yes stops the watch
and then loads, No loads nothing. Loading data can change the active
reconstructor, and a watch must not run on through that.

    pytest imswitch/improcess/_test/test_directory_watcher_load_guard.py -v
"""

from types import SimpleNamespace

import pytest

from imswitch.improcess.controller.FileIOController import FileIOController
from imswitch.improcess.controller.MultiDataFrameController import (
    MultiDataFrameController,
)
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


class _View:
    """The main view's watch surface, with the real confirmLoadingDuringWatch."""

    def __init__(self, *, running, answer):
        self.running = running
        self.answer = answer
        self.asked = []
        self.stopped = 0

    def isDirectoryWatcherRunning(self):
        return self.running

    def confirmDirectoryWatcherInterruption(self, message=None):
        if not self.running:
            return True
        self.asked.append(message)
        return self.answer

    def stopDirectoryWatcher(self):
        self.stopped += 1
        self.running = False

    def confirmLoadingDuringWatch(self):
        return ImProcessMainView.confirmLoadingDuringWatch.__get__(self)()


# --- the question -----------------------------------------------------------

def test_no_watch_means_no_question():
    view = _View(running=False, answer=False)

    assert view.confirmLoadingDuringWatch() is True
    assert (view.asked, view.stopped) == ([], 0)


def test_yes_stops_the_watch_before_the_load():
    view = _View(running=True, answer=True)

    assert view.confirmLoadingDuringWatch() is True
    assert view.stopped == 1
    assert "Loading data will stop it first" in view.asked[0]


def test_no_leaves_the_watch_running_and_loads_nothing():
    view = _View(running=True, answer=False)

    assert view.confirmLoadingDuringWatch() is False
    assert view.stopped == 0


# --- quick load, open virtual, drag and drop --------------------------------

def _file_io(view):
    controller = FileIOController.__new__(FileIOController)
    controller._widget = view
    controller._main = SimpleNamespace(_activeReconstructor=None)
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None)
    picked, loaded = [], []
    controller._requestLoadPath = lambda **k: (picked.append(True), "C:/data/rec.zarr")[1]
    controller._loadFromPath = lambda path, **k: (loaded.append(path), "current")[1]
    view.raiseMultiDataDock = lambda: None
    return controller, picked, loaded


@pytest.mark.parametrize("action", ["quickLoadData", "quickLoadVirtualData"])
def test_declining_a_quick_load_does_not_even_open_the_file_dialog(action):
    controller, picked, loaded = _file_io(_View(running=True, answer=False))

    getattr(controller, action)()

    assert (picked, loaded) == ([], [])


@pytest.mark.parametrize("action", ["quickLoadData", "quickLoadVirtualData"])
def test_accepting_a_quick_load_stops_the_watch_and_loads(action):
    view = _View(running=True, answer=True)
    controller, picked, loaded = _file_io(view)

    getattr(controller, action)()

    assert view.stopped == 1
    assert loaded == ["C:/data/rec.zarr"]


def test_declining_a_drop_loads_none_of_the_files():
    controller, _picked, loaded = _file_io(_View(running=True, answer=False))

    controller.handleDroppedFiles(["C:/a.zarr", "C:/b.zarr"])

    assert loaded == []


def test_a_drop_asks_once_for_all_its_files():
    view = _View(running=True, answer=True)
    controller, _picked, loaded = _file_io(view)

    controller.handleDroppedFiles(["C:/a.zarr", "C:/b.zarr"])

    assert len(view.asked) == 1
    assert loaded == ["C:/a.zarr", "C:/b.zarr"]


# --- the multi-data list ----------------------------------------------------

def _multi_data(view):
    controller = MultiDataFrameController.__new__(MultiDataFrameController)
    controller._mainView = view
    controller._dataFolder = None
    calls = []
    data_obj = SimpleNamespace(checkAndLoadData=lambda: calls.append("load"))
    controller._widget = SimpleNamespace(
        requestFilePathsFromUser=lambda folder: (calls.append("dialog"), [])[1],
        getSelectedDataObjs=lambda: [data_obj],
        getAllDataObjs=lambda: [data_obj],
        getSelectedDataObj=lambda: data_obj,
        setAllRowsHighlighted=lambda value: None,
        setCurrentRowHighlighted=lambda value: None,
    )
    controller._commChannel = SimpleNamespace(
        sigCurrentDataChanged=SimpleNamespace(emit=lambda obj: calls.append("current"))
    )
    controller.updateInfo = lambda: None
    return controller, calls


@pytest.mark.parametrize(
    "action", ["addDataClicked", "loadCurrData", "loadAllData", "setAsCurrentData"]
)
def test_declining_leaves_the_multi_data_list_untouched(action):
    controller, calls = _multi_data(_View(running=True, answer=False))

    getattr(controller, action)()

    assert calls == []


@pytest.mark.parametrize(
    "action, expected",
    [
        ("addDataClicked", ["dialog"]),
        ("loadCurrData", ["load"]),
        ("loadAllData", ["load"]),
        ("setAsCurrentData", ["load", "current"]),
    ],
)
def test_accepting_stops_the_watch_and_does_what_was_asked(action, expected):
    view = _View(running=True, answer=True)
    controller, calls = _multi_data(view)

    getattr(controller, action)()

    assert view.stopped == 1
    assert calls == expected


def test_a_multi_data_list_without_the_main_view_just_loads():
    controller, calls = _multi_data(None)

    controller.loadCurrData()

    assert calls == ["load"]


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

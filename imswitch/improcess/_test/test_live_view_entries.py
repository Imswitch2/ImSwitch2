"""How live reconstruction results become entries in the reconstruction list.

Every job follows the same two-step sequence: its first result creates one data
object named after the job, and every later result updates that same object.
The job is identified by the result's name (the timelapse folder), which
``LiveModeController`` guarantees is unique per job -- so the live path adds no
de-duplicating ``.N`` suffix.

These tests drive the real ``_renderPendingLiveResult`` against the real
``addNewData``/``addNamedData``, so the naming and item bookkeeping under test
are the shipping ones.

``reconList`` is a stand-in rather than a real ``QListWidget``: constructing
one crashes the test process in this environment (there is no pytest-qt /
offscreen widget support here, which is also why the ``qtbot`` tests error).
Real ``QListWidgetItem``s are fine and are what the view actually creates, so
only the list container is faked -- with the slice of the API the view and
controller touch.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.view.ReconstructionView import ReconstructionView


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _FakeList:
    """The slice of QListWidget that the view and controller use."""

    def __init__(self):
        self._items = []
        self._current = None

    def count(self):
        return len(self._items)

    def item(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def addItem(self, item):
        self._items.append(item)

    def setCurrentItem(self, item):
        self._current = item

    def currentItem(self):
        return self._current

    def row(self, item):
        for index, existing in enumerate(self._items):
            if existing is item:
                return index
        return -1

    def clear(self):
        self._items = []
        self._current = None


class _Widget:
    """Minimal stand-in carrying the real list-entry methods."""

    addNewData = ReconstructionView.addNewData
    addNamedData = ReconstructionView.addNamedData

    def __init__(self):
        self.reconList = _FakeList()
        self.fastUpdates = 0

    def tryFastLiveUpdate(self, result, transposeOrder):
        self.fastUpdates += 1
        return True

    def removeAllRecon(self):
        self.reconList.clear()


class _Stub:
    """Weakref-able controller stand-in.

    Not ``SimpleNamespace``: ``liveTimepointUpdated`` schedules the redraw with
    ``QTimer.singleShot``, which takes a weak reference to the bound method's
    object -- and SimpleNamespace does not support weak references.
    """


def _controller():
    _app()
    stub = _Stub()
    stub._widget = _Widget()
    stub._pendingLiveResult = None
    stub._liveRenderScheduled = False
    stub._liveEstablished = False
    stub._liveLatestTimepoint = None
    stub._liveItem = None
    stub._liveName = None
    stub._transposeOrder = [0, 1, 2, 3, 4, 5]
    stub.fullUpdates = 0
    stub._logger = MagicMock()
    stub.fullUpdate = lambda **kwargs: None
    stub._advanceLiveTimeSlider = lambda: None
    # resultProduced notifies the channel that the set of loaded results
    # changed; the entry-naming behaviour under test does not depend on it.
    stub._resultsChanged = lambda: None
    stub._liveItemIsAlive = ReconstructionViewController._liveItemIsAlive.__get__(stub)
    # Bound under its real name too: liveTimepointUpdated schedules it by name.
    stub._renderPendingLiveResult = (
        ReconstructionViewController._renderPendingLiveResult.__get__(stub)
    )
    stub._render = stub._renderPendingLiveResult
    stub._produced = ReconstructionViewController.resultProduced.__get__(stub)
    stub.liveTimepointUpdated = (
        ReconstructionViewController.liveTimepointUpdated.__get__(stub)
    )
    return stub


def _result(name):
    return SimpleNamespace(name=name)


def _live(stub, name):
    """Deliver one live result, as the coalescing timer would."""
    stub._pendingLiveResult = _result(name)
    stub._render()


def _labels(stub):
    lst = stub._widget.reconList
    return [lst.item(i).data(0) for i in range(lst.count())]


# --------------------------------------------------------------------------

def test_live_entry_is_named_exactly_after_the_job():
    """No de-duplicating suffix: the job name is already unique."""
    c = _controller()

    _live(c, "timelapse_00")

    assert _labels(c) == ["timelapse_00"]


def test_repeated_results_for_one_job_reuse_its_entry():
    c = _controller()

    for _ in range(5):
        _live(c, "timelapse_00")

    assert _labels(c) == ["timelapse_00"]


def test_each_job_gets_its_own_entry_via_the_same_sequence():
    """Three jobs, same operations each time -> three entries, one per job."""
    c = _controller()

    for job in ("timelapse_00", "timelapse_01", "timelapse_02"):
        for _ in range(3):
            _live(c, job)

    assert _labels(c) == ["timelapse_00", "timelapse_01", "timelapse_02"]


def test_update_lands_in_its_own_job_entry_not_the_selected_one():
    """Selecting another entry mid-run must not divert or duplicate the run.

    The run holds its own item, so its results keep updating that item; the
    entry the user is inspecting is left exactly as it was.
    """
    c = _controller()
    _live(c, "timelapse_00")
    _live(c, "timelapse_01")

    lst = c._widget.reconList
    lst.setCurrentItem(lst.item(0))              # user clicks timelapse_00
    untouched = lst.item(0).data(1)

    latest = _result("timelapse_01")
    c._pendingLiveResult = latest
    c._render()

    assert _labels(c) == ["timelapse_00", "timelapse_01"]   # no duplicate added
    assert lst.item(0).data(1) is untouched                 # other entry intact
    assert lst.item(1).data(1) is latest                    # run's entry updated


def test_no_redraw_while_the_user_inspects_another_entry():
    """Rendering targets the selection, so skip it when that isn't the run's."""
    c = _controller()
    _live(c, "timelapse_00")
    _live(c, "timelapse_00")          # establishes layers, one fast update
    established = c._widget.fastUpdates

    lst = c._widget.reconList
    other = QtWidgets.QListWidgetItem("something else")
    lst.addItem(other)
    lst.setCurrentItem(other)

    _live(c, "timelapse_00")

    assert c._widget.fastUpdates == established   # no redraw of someone else's view


def test_run_recreates_its_entry_if_the_user_deleted_it():
    """removeAllRecon() deletes the item; the next result starts a fresh one."""
    c = _controller()
    _live(c, "timelapse_00")
    _live(c, "timelapse_00")

    c._widget.removeAllRecon()
    assert _labels(c) == []

    _live(c, "timelapse_00")

    assert _labels(c) == ["timelapse_00"]


def test_run_survives_a_deleted_item_whose_wrapper_raises():
    """removeAllRecon() deletes the C++ item; touching the wrapper then raises.

    ``_liveItemIsAlive`` has to treat that as "gone" rather than propagate, so
    the run recovers with a fresh entry.
    """
    c = _controller()
    _live(c, "timelapse_00")

    class _Exploding(_FakeList):
        def row(self, item):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    exploded = _Exploding()
    c._widget.reconList = exploded

    _live(c, "timelapse_00")

    assert [exploded.item(i).data(0) for i in range(exploded.count())] == ["timelapse_00"]


def test_batch_path_still_deduplicates_repeated_names():
    """resultProduced keeps the .N suffix -- batch names can legitimately repeat."""
    c = _controller()

    c._produced(_result("rec"), "rec")
    c._produced(_result("rec"), "rec")

    assert _labels(c) == ["rec.0", "rec.1"]


# --- incremental timepoint updates -----------------------------------------
#
# After the first whole result establishes the entry, each refresh delivers one
# timepoint plane that is written into the result's own buffer. The result owns
# its array (the session handed over a copy), so this is a GUI-thread-only
# write -- the process thread never touches this memory.


class _LiveResult:
    """Minimal ProcessingResult stand-in with a writable multi-timepoint buffer."""

    def __init__(self, name, n_timepoints=4, shape=(3, 3)):
        self.name = name
        self.axis_labels = ["Dataset", "Base", "T", "Z", "Y", "X"]
        self.data = np.zeros((1, 1, n_timepoints, 1, *shape), dtype=np.float32)


def _plane(value, shape=(3, 3)):
    return np.full((1, 1, 1, 1, *shape), value, dtype=np.float32)


def _established(name="timelapse_00"):
    """A controller whose live entry already holds a result buffer."""
    c = _controller()
    c._pendingLiveResult = _LiveResult(name)
    c._render()
    return c


def test_timepoint_plane_is_written_into_the_live_results_buffer():
    c = _established()
    result = c._liveItem.data(1)

    c.liveTimepointUpdated(2, _plane(7.0))

    assert np.all(result.data[:, :, 2] == 7.0)
    assert np.all(result.data[:, :, 0] == 0.0)   # other timepoints untouched


def test_successive_planes_all_land_even_though_redraws_coalesce():
    """Plane data must be applied immediately -- coalescing would drop data.

    Unlike whole results, successive planes carry *different* timepoints, so
    keeping only the newest would silently lose reconstructed data.
    """
    c = _established()
    result = c._liveItem.data(1)

    for t in range(4):                    # no event loop runs between these
        c.liveTimepointUpdated(t, _plane(t + 1))

    for t in range(4):
        assert np.all(result.data[:, :, t] == t + 1), t
    assert c._liveRenderScheduled is True  # one render pending, not four


def test_timepoint_update_before_any_result_is_ignored():
    """A plane with no established buffer to write into must not raise."""
    c = _controller()
    c.liveTimepointUpdated(0, _plane(1.0))
    assert _labels(c) == []


def test_timepoint_update_survives_a_result_without_a_T_axis():
    c = _established()
    c._liveItem.data(1).axis_labels = ["Y", "X"]
    c.liveTimepointUpdated(0, _plane(1.0))     # must not raise


def test_timepoint_update_survives_an_out_of_range_index():
    c = _established()          # buffer has 4 timepoints
    c.liveTimepointUpdated(99, _plane(1.0))    # must not raise


def test_timepoint_plane_write_logs_nothing_on_the_happy_path():
    """liveTimepointUpdated swallows write failures into a debug line, so a
    silently-degraded run looks identical to a healthy one. Assert the log."""
    c = _established()

    c.liveTimepointUpdated(1, _plane(3.0))

    assert c._logger.debug.call_args_list == []
    assert np.all(c._liveItem.data(1).data[:, :, 1] == 3.0)


def test_timepoint_plane_write_logs_when_the_plane_does_not_fit():
    """Positive control: the guard above only means something if this logs."""
    c = _established()

    c.liveTimepointUpdated(0, np.zeros((1, 1, 1, 1, 99, 99), dtype=np.float32))

    assert len(c._logger.debug.call_args_list) == 1


def test_timepoint_plane_write_logs_when_the_result_has_no_time_axis():
    c = _established()
    c._liveItem.data(1).axis_labels = ["Y", "X"]

    c.liveTimepointUpdated(0, _plane(1.0))

    assert len(c._logger.debug.call_args_list) == 1


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

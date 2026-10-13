"""DatasetResolver (DR): entry -> (T, *S, Y, X), or not raw data, or pending.

Real ImControl output comes from ``_lapse_recordings`` (the production storers);
edge cases use small containers built here.

    pytest imswitch/improcess/_test/test_dataset_resolver.py -v
"""

import os

import h5py
import numpy as np
import pytest
import zarr
from qtpy import QtCore, QtWidgets

from imswitch.improcess._test import _lapse_recordings as L
from imswitch.improcess.controller.DirectoryWatcherController import (
    DirectoryWatcherController,
)
from imswitch.improcess.live.dataset_resolver import DatasetResolver


def resolve(entry):
    return DatasetResolver().resolve(entry)

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

FORMATS = ["hdf5", "zarr"]


def _container(path, arrays, fmt="hdf5"):
    """A container holding ``{array_path: (shape, attrs)}``."""
    path = str(path)
    if fmt == "hdf5":
        with h5py.File(path, "w") as f:
            for name, (shape, attrs) in arrays.items():
                ds = f.create_dataset(name, shape=shape, dtype="uint16")
                ds.attrs.update(attrs)
    else:
        root = zarr.open_group(path, mode="w")
        for name, (shape, attrs) in arrays.items():
            a = root.create_array(name, shape=shape, dtype="uint16")
            a.attrs.update(attrs)
    return path


def _ext(fmt):
    return "hdf5" if fmt == "hdf5" else "zarr"


def _dataset(entry):
    dataset = resolve(str(entry))
    assert dataset is not None, "still pending"
    assert not isinstance(dataset, str), dataset
    return dataset


# --- real ImControl output --------------------------------------------------

@pytest.mark.parametrize("fmt", FORMATS)
def test_p3_camera_lapse(tmp_path, fmt):
    folder = tmp_path / "lapse"
    L.record_lapse(folder, 3, fmt=fmt, camera=True, frames=1)

    d = _dataset(folder)

    assert d.packaging == "P3" and d.format == fmt
    assert d.T == 3
    assert d.S == (("s0", 1),)
    assert d.frame_shape == L.FRAME_SHAPE
    assert os.path.basename(d.pieces[0][0]).startswith("12h00m00s_rec_time0")


@pytest.mark.parametrize("fmt", FORMATS)
def test_p3_scan_lapse(tmp_path, fmt):
    folder = tmp_path / "lapse"
    L.record_lapse(folder, 2, fmt=fmt, camera=False, frames=4, layout_kind="time")

    d = _dataset(folder)

    assert d.packaging == "P3"
    assert (d.T, d.S) == (2, (("s0", 4),))
    assert d.frames_per_timepoint == 4


@pytest.mark.parametrize("fmt", FORMATS)
def test_p2_single_file_lapse(tmp_path, fmt):
    paths = L.record_lapse(tmp_path, 3, fmt=fmt, camera=True, frames=2,
                           single_file=True)

    d = _dataset(paths[0])

    assert d.packaging == "P2"
    assert (d.T, d.S) == (3, (("s0", 2),))
    assert [array for _, array in d.pieces] == [
        "scan0/Camera/data", "scan1/Camera/data", "scan2/Camera/data",
    ]


@pytest.mark.parametrize("fmt", FORMATS)
def test_p1_plain_recording_is_one_timepoint(tmp_path, fmt):
    """The user's rule: no frames-per-timepoint metadata -> T = 1, S = (N,)."""
    path = _container(tmp_path / f"rec.{_ext(fmt)}",
                      {"Camera/data": ((5, 8, 8), {"writing": False})}, fmt)

    d = _dataset(path)

    assert d.packaging == "P1"
    assert (d.T, d.S, d.frame_shape) == (1, (("s0", 5),), (8, 8))


def test_p1_with_frames_per_stack_is_a_run_of_stacks(tmp_path):
    path = _container(tmp_path / "rec.h5", {"Camera/data": ((12, 8, 8), {
        "recording:frames_per_stack": 4, "recording:expected_frames": 12,
        "writing": False,
    })})

    d = _dataset(path)

    assert (d.T, d.S) == (3, (("s0", 4),))


# --- packaging is decided by the index, not by counting ---------------------

def test_a_single_file_lapse_with_only_its_first_group_is_still_p2(tmp_path):
    """A lapse being recorded: scan0 exists, scan1.. do not yet."""
    path = _container(tmp_path / "rec.h5", {"scan0/Camera/data": ((2, 8, 8), {
        "recording:frames_per_stack": 2, "recording:num_timepoints": 5,
    })})

    d = _dataset(path)

    assert d.packaging == "P2"
    assert (d.T, d.S) == (5, (("s0", 2),))


def test_a_folder_with_one_container_is_still_p3(tmp_path):
    folder = tmp_path / "lapse"
    folder.mkdir()
    _container(folder / "rec_time0_Cam.h5", {"Cam/data": ((1, 8, 8), {
        "recording:frames_per_stack": 1,
    })})

    assert _dataset(folder).packaging == "P3"


def test_p3_pieces_are_ordered_by_index_not_by_name(tmp_path):
    """Unpadded indices: name order puts time10 before time2."""
    folder = tmp_path / "lapse"
    folder.mkdir()
    for i in (10, 2, 0):
        _container(folder / f"rec_time{i}_Cam.h5", {"Cam/data": ((1, 8, 8), {
            "recording:frames_per_stack": 1,
        })})

    first_container = _dataset(folder).pieces[0][0]

    assert os.path.basename(first_container) == "rec_time0_Cam.h5"


def test_bookkeeping_arrays_are_not_data(tmp_path):
    """HDF5 recordings carry 1-D side arrays next to the data."""
    path = str(tmp_path / "rec.h5")
    with h5py.File(path, "w") as f:
        f.create_dataset("Cam/data", shape=(3, 8, 8), dtype="uint16")
        f.create_dataset("Cam/frames_committed", data=np.array([3]))
        f.create_dataset("Cam/stream_complete", data=np.array([1], np.uint8))

    assert _dataset(path).packaging == "P1"


# --- S while the data is still being written --------------------------------

def test_frames_per_stack_gives_s_before_any_frame_is_written(tmp_path):
    path = _container(tmp_path / "rec.h5", {"Cam/data": ((0, 8, 8), {
        "recording:frames_per_stack": 25, "writing": True,
    })})

    assert _dataset(path).S == (("s0", 25),)


def test_without_frames_per_stack_a_growing_array_is_pending(tmp_path):
    """S = (N,) needs N, and N is not final while the array grows."""
    path = _container(tmp_path / "rec.h5",
                      {"Cam/data": ((7, 8, 8), {"writing": True})})

    assert resolve(path) is None


def test_an_array_without_a_writing_marker_counts_as_finished(tmp_path):
    """An external file carries no marker; there is nothing to wait for."""
    path = _container(tmp_path / "rec.h5", {"Cam/data": ((7, 8, 8), {})})

    assert _dataset(path).S == (("s0", 7),)


# --- not raw data -----------------------------------------------------------

def test_a_plain_file_is_not_raw_data(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")

    assert isinstance(resolve(str(tmp_path / "notes.txt")), str)


def test_several_unindexed_arrays_are_not_supported_yet(tmp_path):
    path = _container(tmp_path / "rec.h5", {
        "CamA/data": ((3, 8, 8), {}), "CamB/data": ((3, 8, 8), {}),
    })

    reason = resolve(path)

    assert isinstance(reason, str) and "detectors" in reason


def test_an_entry_that_vanished_is_dropped_not_kept_pending(tmp_path):
    assert isinstance(resolve(str(tmp_path / "gone.h5")), str)


# --- pending ----------------------------------------------------------------

def test_an_empty_folder_is_pending(tmp_path):
    (tmp_path / "lapse").mkdir()

    assert resolve(str(tmp_path / "lapse")) is None


def test_a_container_without_a_data_array_yet_is_pending(tmp_path):
    path = str(tmp_path / "rec.h5")
    with h5py.File(path, "w") as f:
        f.create_group("Cam")

    assert resolve(path) is None


def test_an_unreadable_container_is_pending(tmp_path):
    path = tmp_path / "rec.h5"
    path.write_bytes(b"not an hdf5 file yet")

    assert resolve(str(path)) is None


# --- the DR: keeping and retrying -------------------------------------------

def _resolver():
    dr = DatasetResolver()
    found = []
    dr.sigResolved.connect(found.append)
    return dr, found


def test_a_resolvable_entry_is_emitted_at_once(tmp_path):
    path = _container(tmp_path / "rec.h5", {"Cam/data": ((3, 8, 8), {})})
    dr, found = _resolver()

    dr.add(path)

    assert [d.entry for d in found] == [path]
    assert dr.pending == []


def test_a_pending_entry_is_kept_and_resolved_on_a_later_retry(tmp_path):
    folder = tmp_path / "lapse"
    folder.mkdir()
    dr, found = _resolver()

    dr.add(str(folder))
    assert found == [] and dr.pending == [str(folder)]

    _container(folder / "rec_time0_Cam.h5", {"Cam/data": ((1, 8, 8), {
        "recording:frames_per_stack": 1,
    })})
    dr.retry()

    assert [d.packaging for d in found] == ["P3"]
    assert dr.pending == []


def test_a_resolved_entry_is_not_retried(tmp_path):
    """The DR's only memory is what is pending. Reporting each entry once is
    the DO's job, so the DR does not keep a second list of what it has seen."""
    folder = tmp_path / "lapse"
    folder.mkdir()
    dr, found = _resolver()
    dr.add(str(folder))
    _container(folder / "rec_time0_Cam.h5", {"Cam/data": ((1, 8, 8), {})})

    dr.retry()
    dr.retry()

    assert len(found) == 1


def test_not_raw_data_is_dropped_not_kept(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    dr, found = _resolver()

    dr.add(str(tmp_path / "notes.txt"))

    assert found == [] and dr.pending == []


def test_adding_a_pending_entry_twice_keeps_it_once(tmp_path):
    (tmp_path / "lapse").mkdir()
    dr, _ = _resolver()

    dr.add(str(tmp_path / "lapse"))
    dr.add(str(tmp_path / "lapse"))

    assert dr.pending == [str(tmp_path / "lapse")]


# --- the DWC: DO -> DR ------------------------------------------------------

class _Check:
    def setChecked(self, value):
        self.checked = bool(value)


class _FakeDWW(QtCore.QObject):
    sigMonitorChanged = QtCore.Signal(bool)

    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        self.monitorFolderCheck = _Check()


def test_the_dwc_passes_every_entry_from_the_do_to_the_dr(tmp_path):
    L.record_lapse(tmp_path / "lapseA", 2, fmt="hdf5", camera=True, frames=1)
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "empty").mkdir()
    widget = _FakeDWW(tmp_path)
    dwc = DirectoryWatcherController(None, widget=widget, factory=None,
                                     moduleCommChannel=None)
    resolved = []

    widget.sigMonitorChanged.emit(True)         # first check runs here...
    dwc._resolver.sigResolved.connect(resolved.append)
    (tmp_path / "late").mkdir()
    L.record_lapse(tmp_path / "late", 1, fmt="zarr", camera=True, frames=1)
    dwc._observer.check()                       # ...and the next one here

    assert dwc._resolver.pending == [os.path.join(str(tmp_path), "empty")]
    assert [os.path.basename(d.entry) for d in resolved] == ["late"]


def test_unticking_stops_both(tmp_path):
    widget = _FakeDWW(tmp_path)
    dwc = DirectoryWatcherController(None, widget=widget, factory=None,
                                     moduleCommChannel=None)
    widget.sigMonitorChanged.emit(True)
    assert dwc._observer.running and dwc._resolver.running

    widget.sigMonitorChanged.emit(False)

    assert not dwc._observer.running and not dwc._resolver.running


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

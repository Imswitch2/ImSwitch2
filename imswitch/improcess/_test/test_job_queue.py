"""Standalone tests for ``imswitch.improcess.live.discovery.JobQueue``.

Pure and synchronous -- no Qt, no store opening.

    pytest imswitch/improcess/_test/test_job_queue.py -v
"""

import os

import pytest

from imswitch.improcess.live.discovery import DiscoveredJob, JobQueue, _seed_key


def _store(folder, name):
    """Create a fake .zarr store (a directory) inside ``folder``."""
    os.makedirs(os.path.join(str(folder), name), exist_ok=True)


# --- _seed_key -------------------------------------------------------------

def test_seed_key_orders_timepoint_zero_first_when_padded():
    names = ["r_scan__02__CAM.zarr", "r_scan__00__CAM.zarr", "r_scan__01__CAM.zarr"]
    assert sorted(names, key=_seed_key)[0] == "r_scan__00__CAM.zarr"


def test_seed_key_picks_scan0_even_when_unpadded():
    names = ["r_scan10.zarr", "r_scan2.zarr", "r_scan0.zarr", "r_scan1.zarr"]
    assert sorted(names, key=_seed_key)[0] == "r_scan0.zarr"


def test_seed_key_raises_without_scan_marker():
    with pytest.raises(ValueError):
        _seed_key("recording_00_CAM.zarr")


# --- add ------------------------------------------------------------------

def test_add_is_idempotent():
    q = JobQueue()
    q.add("/data/measA")
    q.add("/data/measA")
    assert len(q) == 1


# --- next_ready ----------------------------------------------------------

def test_next_ready_none_when_empty():
    assert JobQueue().next_ready() is None


def test_next_ready_none_while_folder_has_no_store(tmp_path):
    (tmp_path / "measA").mkdir()
    q = JobQueue()
    q.add(str(tmp_path / "measA"))
    assert q.next_ready() is None
    assert len(q) == 1                       # stays pending


def test_next_ready_resolves_folder_to_seed_job(tmp_path):
    folder = tmp_path / "measA"
    _store(folder, "exp_scan__01__CAM.zarr")
    _store(folder, "exp_scan__00__CAM.zarr")
    _store(folder, "exp_scan__02__CAM.zarr")

    q = JobQueue()
    q.add(str(folder))
    job = q.next_ready()

    assert isinstance(job, DiscoveredJob)
    assert job.folder == str(folder)
    assert job.seed_path == os.path.join(str(folder), "exp_scan__00__CAM.zarr")
    assert len(q) == 0                       # removed once resolved


def test_next_ready_does_not_re_resolve(tmp_path):
    folder = tmp_path / "measA"
    _store(folder, "exp_scan0.zarr")
    q = JobQueue()
    q.add(str(folder))

    assert q.next_ready() is not None
    assert q.next_ready() is None            # already handed out


def test_next_ready_ignores_non_zarr_entries(tmp_path):
    folder = tmp_path / "measA"
    _store(folder, "exp_scan0.zarr")
    (folder / "log.txt").write_text("x")
    (folder / "notes").mkdir()

    q = JobQueue()
    q.add(str(folder))
    assert q.next_ready().seed_path.endswith("exp_scan0.zarr")


def test_next_ready_skips_empty_folder_and_resolves_a_later_one(tmp_path):
    empty = tmp_path / "measA"
    ready = tmp_path / "measB"
    empty.mkdir()
    _store(ready, "exp_scan0.zarr")

    q = JobQueue()
    q.add(str(empty))
    q.add(str(ready))

    job = q.next_ready()
    assert job.folder == str(ready)
    assert len(q) == 1                       # the empty folder stays pending
    assert q._pending_jobs == [str(empty)]


def test_next_ready_skips_a_folder_that_cannot_be_listed(tmp_path):
    gone = tmp_path / "measA"               # never created
    q = JobQueue()
    q.add(str(gone))
    assert q.next_ready() is None
    assert len(q) == 1                       # stays pending, no crash


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

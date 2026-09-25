"""Tests for ``imswitch.improcess.live.source_type``.

Two halves: ``raw_source_type_from_evidence`` is pure (dictionaries in,
classification out) and ``probe_source_type`` is exercised against real Zarr and
HDF5 stores on disk, because the layouts it has to tell apart are structural.

    pytest imswitch/improcess/_test/test_raw_source_type.py -v
"""

import dataclasses

import numpy as np
import pytest

from imswitch.improcess.live.source_type import (
    FORMAT_HDF5,
    FORMAT_ZARR,
    LAYOUT_MULTIFILE_LAPSE,
    LAYOUT_SINGLE,
    LAYOUT_SINGLE_LAPSE_FILE,
    format_id_for_path,
    is_scan_group_name,
    probe_source_type,
    raw_source_type_from_evidence,
)


def _t(attrs, format_id=FORMAT_ZARR, **kwargs):
    return raw_source_type_from_evidence(attrs, format_id, **kwargs)


# --- layout, from metadata -------------------------------------------------

def test_no_evidence_is_a_single_dataset_of_single_frames():
    """Claims the least: right for a snap, a non-lapse recording, and anything
    not written by ImSwitch at all."""
    t = _t({})

    assert (t.layout, t.num_timepoints, t.frames_per_stack) == (LAYOUT_SINGLE, 1, 1)


def test_none_attrs_are_accepted():
    assert _t(None).layout == LAYOUT_SINGLE


def test_lapse_across_files_is_a_multifile_lapse():
    t = _t({"recording:num_timepoints": 10, "recording:single_lapse_file": False})

    assert t.layout == LAYOUT_MULTIFILE_LAPSE


def test_lapse_in_one_file_is_a_single_lapse_file():
    t = _t({"recording:num_timepoints": 10, "recording:single_lapse_file": True})

    assert t.layout == LAYOUT_SINGLE_LAPSE_FILE


def test_the_single_lapse_flag_is_ignored_without_a_lapse():
    t = _t({"recording:num_timepoints": 1, "recording:single_lapse_file": True})

    assert t.layout == LAYOUT_SINGLE


@pytest.mark.parametrize("written,expected", [
    (True, LAYOUT_SINGLE_LAPSE_FILE),
    ("True", LAYOUT_SINGLE_LAPSE_FILE),
    (1, LAYOUT_SINGLE_LAPSE_FILE),
    (False, LAYOUT_MULTIFILE_LAPSE),
    ("False", LAYOUT_MULTIFILE_LAPSE),
    (0, LAYOUT_MULTIFILE_LAPSE),
    (None, LAYOUT_MULTIFILE_LAPSE),
])
def test_single_lapse_file_reads_every_way_it_is_written(written, expected):
    """Zarr round-trips a real bool; HDF5 and older writers store 0/1 or text.
    ``bool("False")`` is True, which is what this guards."""
    attrs = {"recording:num_timepoints": 4}
    if written is not None:
        attrs["recording:single_lapse_file"] = written

    assert _t(attrs).layout == expected


def test_legacy_null_string_counts_as_absent():
    t = _t({"recording:num_timepoints": "null"})

    assert (t.layout, t.num_timepoints) == (LAYOUT_SINGLE, 1)


def test_metadata_nested_under_imswitchdata_is_found():
    """Legacy ImSwitch-1 Zarr nests these keys instead of flattening them."""
    t = _t({"ImswitchData": {"recording:num_timepoints": 6,
                             "recording:frames_per_stack": 400}})

    assert (t.layout, t.num_timepoints, t.frames_per_stack) == (
        LAYOUT_MULTIFILE_LAPSE, 6, 400
    )


# --- layout, from structure ------------------------------------------------

def test_scan_groups_alone_decide_the_layout():
    """Structure beats metadata, and is all a legacy file has."""
    t = _t({}, scan_groups=4)

    assert t.layout == LAYOUT_SINGLE_LAPSE_FILE


def test_scan_groups_floor_the_timepoint_count():
    assert _t({}, scan_groups=7).num_timepoints == 7


def test_recorded_timepoints_win_when_higher_than_the_groups_present():
    """A lapse still being written has fewer groups on disk than it will have."""
    t = _t({"recording:num_timepoints": 10}, scan_groups=3)

    assert t.num_timepoints == 10


@pytest.mark.parametrize("name,expected", [
    ("scan0", True), ("scan17", True), ("scan", False),
    ("scans", False), ("scanA", False), ("Cam", False),
])
def test_is_scan_group_name(name, expected):
    assert is_scan_group_name(name) is expected


# --- frames_per_stack precedence -------------------------------------------

def test_explicit_frames_per_stack_wins_over_scan_geometry():
    assert _t({"recording:frames_per_stack": 99,
               "ScanTTL:Nx": 35, "ScanTTL:Ny": 35}).frames_per_stack == 99


def test_scan_ttl_counts_are_used_when_no_explicit_value():
    assert _t({"ScanTTL:Nx": 35, "ScanTTL:Ny": 35}).frames_per_stack == 35 * 35


def test_the_seed_group_frame_count_is_the_last_resort():
    """A single-file lapse keeps frames inside scanN, so nothing above sees
    them; the seed group's own length is what is left."""
    assert _t({}, scan_groups=3, frames_in_seed=1225).frames_per_stack == 1225


# --- derived properties ----------------------------------------------------

def test_has_frame_stacks_separates_a_scan_from_a_camera_recording():
    scan = _t({"recording:frames_per_stack": 1225, "recording:num_timepoints": 10})
    camera = _t({"recording:expected_frames": 1, "recording:num_timepoints": 50})

    assert scan.has_frame_stacks and not camera.has_frame_stacks
    # both are lapses -- being a timelapse says nothing about frame stacks
    assert scan.is_timelapse and camera.is_timelapse


def test_total_frames_spans_the_whole_recording():
    assert _t({"recording:frames_per_stack": 100,
               "recording:num_timepoints": 7}).total_frames == 700


def test_source_type_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _t({}).layout = LAYOUT_SINGLE_LAPSE_FILE


# --- format from the name --------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("root/a.zarr", FORMAT_ZARR), ("root/a.zarr/", FORMAT_ZARR),
    ("root/a.ZARR", FORMAT_ZARR), ("root/a.h5", FORMAT_HDF5),
    ("root/a.hdf5", FORMAT_HDF5), ("root/a.hdf", FORMAT_HDF5),
    ("root/a.tif", None), ("root/a.ome.tiff", None), ("root/a", None),
])
def test_format_id_for_path(path, expected):
    assert format_id_for_path(path) == expected


def test_format_id_needs_no_filesystem():
    assert format_id_for_path("/nonexistent/x.zarr") == FORMAT_ZARR


# --- probe_source_type, against real stores --------------------------------

def _zarr_single(path, *, writing=False, frames=4, **recording):
    import zarr

    group = zarr.open_group(str(path), mode="w")
    array = group.create_array("Cam", shape=(frames, 8, 8), dtype="uint16")
    array.attrs["writing"] = writing
    for key, value in recording.items():
        array.attrs[f"recording:{key}"] = value
    return str(path)


def _zarr_lapse_file(path, *, groups=3, frames=1225, **recording):
    import zarr

    root = zarr.open_group(str(path), mode="w")
    for i in range(groups):
        array = root.create_group(f"scan{i}").create_array(
            "Cam", shape=(frames, 8, 8), dtype="uint16"
        )
        array.attrs["writing"] = False
        for key, value in recording.items():
            array.attrs[f"recording:{key}"] = value
    return str(path)


def _hdf5_lapse_file(path, *, groups=5, frames=400):
    import h5py

    with h5py.File(str(path), "w", libver="latest") as handle:
        for i in range(groups):
            handle.create_group(f"scan{i}").create_dataset(
                "Cam", data=np.zeros((frames, 8, 8), "uint16")
            )
    return str(path)


def test_probe_reads_a_multifile_lapse_seed(tmp_path):
    path = _zarr_single(tmp_path / "m_scan00.zarr", num_timepoints=10,
                        frames_per_stack=4, single_lapse_file=False,
                        detector_name="Cam")

    t = probe_source_type(path)

    assert (t.format_id, t.layout) == (FORMAT_ZARR, LAYOUT_MULTIFILE_LAPSE)
    assert (t.frames_per_stack, t.num_timepoints) == (4, 10)


def test_probe_reads_a_single_dataset(tmp_path):
    path = _zarr_single(tmp_path / "snap.zarr", detector_name="Cam")

    assert probe_source_type(path).layout == LAYOUT_SINGLE


def test_probe_reads_a_zarr_lapse_file_with_no_metadata(tmp_path):
    """Everything here comes from structure: the group count is the timepoint
    total, and the seed group's length is the stack size. Without the latter a
    scanning recording stored this way would report one frame per timepoint and
    be judged incompatible with a scanning reconstructor."""
    path = _zarr_lapse_file(tmp_path / "g.zarr")

    t = probe_source_type(path)

    assert t.layout == LAYOUT_SINGLE_LAPSE_FILE
    assert (t.num_timepoints, t.frames_per_stack) == (3, 1225)
    assert t.has_frame_stacks


def test_probe_reads_an_hdf5_lapse_file_with_no_metadata(tmp_path):
    path = _hdf5_lapse_file(tmp_path / "g.hdf5")

    t = probe_source_type(path)

    assert (t.format_id, t.layout) == (FORMAT_HDF5, LAYOUT_SINGLE_LAPSE_FILE)
    assert (t.num_timepoints, t.frames_per_stack) == (5, 400)


def test_probe_uses_the_array_length_when_a_store_says_nothing(tmp_path):
    path = _zarr_single(tmp_path / "bare.zarr", frames=12)

    assert probe_source_type(path).frames_per_stack == 12


# --- "not yet" is not an error ---------------------------------------------

def test_probe_defers_a_store_still_being_written(tmp_path):
    path = _zarr_single(tmp_path / "wip.zarr", writing=True)

    assert probe_source_type(path) is None


def test_probe_defers_a_missing_entry(tmp_path):
    assert probe_source_type(str(tmp_path / "gone.zarr")) is None


def test_probe_rejects_a_format_we_do_not_read(tmp_path):
    path = tmp_path / "x.tif"
    path.write_bytes(b"")

    assert probe_source_type(str(path)) is None


def test_probe_never_raises_on_a_corrupt_store(tmp_path):
    path = tmp_path / "junk.hdf5"
    path.write_bytes(b"not an hdf5 file at all")

    assert probe_source_type(str(path)) is None


def test_probe_holds_nothing_open(tmp_path):
    """On Windows an open handle blocks the delete, so this is a real check."""
    import os

    path = _hdf5_lapse_file(tmp_path / "e.hdf5", groups=2, frames=4)
    assert probe_source_type(path) is not None

    os.remove(path)
    assert not os.path.exists(path)



# --- sibling files: the evidence a lapse actually leaves --------------------

def _lapse_folder(folder, indices, *, meta=True, frames=1225):
    """A folder of per-timepoint stores, as the recorder writes them."""
    import zarr

    folder.mkdir(parents=True, exist_ok=True)
    seed = None
    for i in indices:
        path = folder / f"rec_scan{i:02d}_Cam.zarr"
        array = zarr.open_group(str(path), mode="w").create_array(
            "Cam", shape=(frames, 8, 8), dtype="uint16"
        )
        array.attrs["writing"] = False
        array.attrs["recording:detector_name"] = "Cam"
        array.attrs["recording:frames_per_stack"] = frames
        if meta:
            array.attrs["recording:num_timepoints"] = len(indices)
        if seed is None:
            seed = str(path)
    return seed


def test_siblings_alone_identify_a_multifile_lapse(tmp_path):
    """The regression: a seed whose recorder stamped no lapse metadata still
    has its siblings on disk. Without reading them the folder classified as a
    single dataset, the single-store reader was chosen, and exactly one
    timepoint of each timelapse was reconstructed."""
    seed = _lapse_folder(tmp_path / "lapseA", range(3), meta=False)

    t = probe_source_type(seed)

    assert t.layout == LAYOUT_MULTIFILE_LAPSE
    assert t.num_timepoints == 3


def test_siblings_and_metadata_agree(tmp_path):
    seed = _lapse_folder(tmp_path / "lapseB", range(3), meta=True)

    assert probe_source_type(seed).layout == LAYOUT_MULTIFILE_LAPSE


def test_a_gap_does_not_shorten_the_span(tmp_path):
    """t3 never arrived; the run still spans t0..t4 so the blank keeps its
    slot and the viewer's slider skips it."""
    seed = _lapse_folder(tmp_path / "gapped", [0, 1, 2, 4], meta=False)

    assert probe_source_type(seed).num_timepoints == 5


def test_the_recorded_total_wins_over_what_is_on_disk_so_far(tmp_path):
    """A lapse still being written has fewer files than it will have."""
    import zarr

    folder = tmp_path / "growing"
    folder.mkdir()
    seed = None
    for i in range(2):
        path = folder / f"rec_scan{i:02d}_Cam.zarr"
        array = zarr.open_group(str(path), mode="w").create_array(
            "Cam", shape=(4, 8, 8), dtype="uint16"
        )
        array.attrs["writing"] = False
        array.attrs["recording:detector_name"] = "Cam"
        array.attrs["recording:num_timepoints"] = 10
        seed = seed or str(path)

    assert probe_source_type(seed).num_timepoints == 10


def test_a_lone_store_without_a_scan_index_stays_single(tmp_path):
    folder = tmp_path / "solo"
    folder.mkdir()
    path = _zarr_single(folder / "measurement.zarr", detector_name="Cam")

    assert probe_source_type(path).layout == LAYOUT_SINGLE


def test_sibling_span_is_evidence_in_the_pure_classifier():
    assert _t({}, sibling_span=4).layout == LAYOUT_MULTIFILE_LAPSE
    assert _t({}, sibling_span=4).num_timepoints == 4
    assert _t({}, sibling_span=1).layout == LAYOUT_SINGLE


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

"""A time lapse opens as one source, from any of its items.

Recorded through the production storers (see ``_lapse_recordings``), then read
back through ``improcess.model.lapse_source``. What is pinned here:

- any item of a lapse resolves to the whole lapse, from its recorded
  attributes; a single-file lapse is opened and listed once, not once per point;
- only real time is stacked: a tiling run -- new-style ``tile`` partitions or an
  old folder with ``tiles.json`` -- and a positioned lapse are refused;
- points go where their recorded index says, so a missing point is a gap and
  not a shift, an incomplete point is marked, and a shape change is refused;
- nothing is read until something indexes the stack, and then only that.
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from imswitch.improcess._test._lapse_recordings import (
    FRAME_SHAPE,
    record_lapse,
    record_point,
)
from imswitch.improcess.model import lapse_source
from imswitch.improcess.model.lapse_source import (
    INCOMPLETE_SKIP,
    STORAGE_ONE_FILE_PER_ITEM,
    STORAGE_ONE_GROUP_PER_ITEM,
    TIMEPOINT_COMPLETE,
    TIMEPOINT_INCOMPLETE,
    TIMEPOINT_MISSING,
    LazyTimeLapseArray,
    NotATimeLapse,
    TimeLapseShapeError,
    discover_time_lapse,
    lapses_in_file,
    open_time_lapse,
    plan_time_lapse,
    read_lapse_headers,
)

MULTI_FILE_FORMATS = ("hdf5", "zarr", "ome.tiff")
SINGLE_FILE_FORMATS = ("hdf5", "zarr")


def _plan(path, **kwargs):
    index = discover_time_lapse(path)
    return plan_time_lapse(index, read_lapse_headers(index), **kwargs)


def _values(stack):
    """The value each plane holds: its point's index + 1, or 0 when blank."""
    return [int(np.asarray(stack[t]).max()) for t in range(len(stack))]


# --------------------------------------------------------------------------
# finding the lapse
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", MULTI_FILE_FORMATS)
@pytest.mark.parametrize("opened", [0, 6, 9])
def test_any_item_of_a_camera_lapse_opens_the_whole_lapse(tmp_path, fmt, opened):
    paths = record_lapse(tmp_path, 10, fmt=fmt)

    index = discover_time_lapse(paths[opened])

    assert index.storage == STORAGE_ONE_FILE_PER_ITEM
    assert index.planned_count == 10
    assert index.anchor_index == opened
    assert index.interval_s == 10.0
    assert index.name == "12h00m00s_rec"
    # The index is padded to the planned count: ``_time06`` for ten points.
    assert paths[6].name.startswith("12h00m00s_rec_time06_")
    assert [ref.path for ref in index.channel().candidates] == paths


@pytest.mark.parametrize("fmt", MULTI_FILE_FORMATS)
def test_a_camera_lapse_stacks_one_plane_per_point(tmp_path, fmt):
    paths = record_lapse(tmp_path, 10, fmt=fmt)

    plan = _plan(paths[3])
    stack = LazyTimeLapseArray(plan)

    # A camera point is a one-frame recording; its frame axis is not kept.
    assert stack.shape == (10, *FRAME_SHAPE)
    assert plan.item_axis_labels == ("Y", "X")
    assert plan.item_axis_scales == (0.2, 0.1)
    assert _values(stack) == list(range(1, 11))


def test_a_scan_lapse_is_found_by_its_scan_token(tmp_path):
    paths = record_lapse(tmp_path, 4, camera=False, frames=6, layout_kind="time")

    plan = _plan(paths[1])

    assert paths[1].name == "12h00m00s_rec_scan1_Camera.hdf5"
    assert plan.shape == (4, 6, *FRAME_SHAPE)
    # The item's own frame axis is not called T: the stack's T is the lapse.
    assert plan.item_axis_labels == ("Frame", "Y", "X")


@pytest.mark.parametrize("fmt", SINGLE_FILE_FORMATS)
def test_a_single_file_lapse_opens_as_one_source(tmp_path, fmt):
    paths = record_lapse(
        tmp_path, 5, fmt=fmt, camera=False, single_file=True, frames=6,
        layout_kind="time",
    )
    assert len(set(paths)) == 1

    plan = _plan(paths[0])
    stack = LazyTimeLapseArray(plan)

    assert plan.index.storage == STORAGE_ONE_GROUP_PER_ITEM
    assert plan.index.name == "12h00m00s_rec"
    assert stack.shape == (5, 6, *FRAME_SHAPE)
    assert _values(stack) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("fmt", SINGLE_FILE_FORMATS)
def test_a_single_file_lapse_is_opened_and_listed_once(tmp_path, fmt, monkeypatch):
    """What froze: one full open and listing of the whole file per point."""
    path = record_lapse(
        tmp_path, 6, fmt=fmt, camera=False, single_file=True, frames=2,
        layout_kind="time",
    )[0]
    opened = []
    listed = []
    real_open = lapse_source._open_container
    real_names = lapse_source.dataset_names
    monkeypatch.setattr(
        lapse_source, "_open_container",
        lambda p: opened.append(p) or real_open(p),
    )
    monkeypatch.setattr(
        lapse_source, "dataset_names",
        lambda c: listed.append(c) or real_names(c),
    )

    index = discover_time_lapse(path)
    headers = read_lapse_headers(index)
    stack = LazyTimeLapseArray(plan_time_lapse(index, headers))
    _values(stack)

    # Discovery, the header pass, and every read the stack makes: one open each.
    assert len(opened) == 3
    # No full dataset listing at all: the per-point groups are found by name.
    assert listed == []


def test_a_second_lapse_recorded_into_the_same_file_is_its_own_lapse(tmp_path):
    """Groups are numbered by the next free one, not by the lapse index."""
    common = dict(camera=False, single_file=True, frames=2, layout_kind="time")
    record_lapse(tmp_path, 3, **common)
    path = record_lapse(
        tmp_path, 3, started_at=None, extra_attrs={"note": "second"}, **common
    )[0]

    groups = [ref.dataset for ref in discover_time_lapse(path).channel().candidates]
    assert groups == [f"scan{n}/Camera" for n in range(6)]

    first = plan_time_lapse(
        discover_time_lapse(path, "scan1/Camera"),
        read_lapse_headers(discover_time_lapse(path, "scan1/Camera")),
    )
    second_index = discover_time_lapse(path, "scan4/Camera")
    second = plan_time_lapse(second_index, read_lapse_headers(second_index))

    assert [slot.dataset for slot in first.slots] == [
        "scan0/Camera", "scan1/Camera", "scan2/Camera"
    ]
    assert [slot.dataset for slot in second.slots] == [
        "scan3/Camera", "scan4/Camera", "scan5/Camera"
    ]
    assert len(second.excluded) == 3


def _two_lapses_in_one_file(folder, fmt="hdf5"):
    common = dict(camera=False, single_file=True, frames=2, layout_kind="time", fmt=fmt)
    record_lapse(folder, 3, **common)
    return record_lapse(folder, 3, started_at=None, **common)[0]


@pytest.mark.parametrize("fmt", SINGLE_FILE_FORMATS)
def test_the_lapses_in_one_file_are_counted_by_the_planning_rule(tmp_path, fmt):
    one = record_lapse(tmp_path / "one", 3, camera=False, single_file=True,
                       frames=2, layout_kind="time", fmt=fmt)[0]
    two = _two_lapses_in_one_file(tmp_path / "two", fmt)

    assert lapses_in_file(discover_time_lapse(one)) == 1
    assert lapses_in_file(discover_time_lapse(two)) == 2


def test_a_multi_file_lapse_is_one_lapse(tmp_path):
    path = record_lapse(tmp_path, 3)[1]
    assert lapses_in_file(discover_time_lapse(path)) == 1


@pytest.mark.parametrize("picked", ["scan4", "scan4/Camera", "scan4/Camera/data", "/scan4/Camera/"])
def test_a_path_inside_a_lapse_group_names_that_groups_item(tmp_path, picked):
    """A path picked inside the container may stop at the group or go below
    the detector; the item is the group's detector either way."""
    path = _two_lapses_in_one_file(tmp_path)

    index = discover_time_lapse(path, picked)

    assert index.anchor.dataset == "scan4/Camera"
    assert index.anchor_detector == "Camera"
    plan = plan_time_lapse(index, read_lapse_headers(index))
    assert [slot.dataset for slot in plan.slots] == [
        "scan3/Camera", "scan4/Camera", "scan5/Camera"
    ]


def test_a_file_named_like_the_lapse_but_from_another_is_left_out(tmp_path):
    """The name finds candidates; only the recorded attributes admit them."""
    paths = record_lapse(tmp_path, 3)
    # Named as this lapse's point 2, but recorded by a 12-point lapse.
    paths[2].unlink()
    other = record_point(tmp_path / "other", index=2, total=3,
                         extra_attrs={"recording:num_timepoints": 12})
    shutil.move(str(other), str(paths[2]))

    index = discover_time_lapse(paths[0])
    plan = plan_time_lapse(index, read_lapse_headers(index))

    assert [slot.lapse_index for slot in plan.slots] == [0, 1]
    assert [reason for _ref, reason in plan.excluded] == [
        "belongs to a 12-point lapse, not this 3-point one"
    ]


# --------------------------------------------------------------------------
# only real time is stacked
# --------------------------------------------------------------------------


def test_a_single_recording_is_not_a_lapse(tmp_path):
    path = record_point(tmp_path, index=0, total=1)

    with pytest.raises(NotATimeLapse, match="single recording"):
        discover_time_lapse(path)


@pytest.mark.parametrize(
    ("kind", "message"),
    [("tile", "Tiling mosaic"), ("position", "positions, not timepoints")],
)
def test_a_positioned_lapse_is_not_stacked_as_time(tmp_path, kind, message):
    path = record_lapse(
        tmp_path, 3, camera=False, frames=2, layout_kind=kind
    )[1]

    with pytest.raises(NotATimeLapse, match=message):
        discover_time_lapse(path)


def test_a_tiling_point_without_a_layout_is_not_time(tmp_path):
    path = record_lapse(
        tmp_path, 3, extra_attrs={"Tiling:grid_x": 0, "Tiling:grid_y": 1}
    )[0]

    with pytest.raises(NotATimeLapse, match="tile of a tiling run"):
        discover_time_lapse(path)


def test_an_old_tiling_folder_belongs_to_the_tiling_reconstructor(tmp_path):
    """Before partition kinds, tiling payloads were labelled ``time``."""
    run = tmp_path / "tiling_20260801_120000"
    path = record_lapse(run, 3, camera=False, frames=2, layout_kind="time")[0]
    (run / "tiles.json").write_text(
        json.dumps({"format": "imswitch-tiling/2", "tiles": []}), encoding="utf-8"
    )

    with pytest.raises(NotATimeLapse, match="tiling run"):
        discover_time_lapse(path)


def test_a_renamed_item_cannot_find_its_siblings(tmp_path):
    path = record_lapse(tmp_path, 3)[1]
    renamed = path.with_name("renamed_Camera.hdf5")
    path.rename(renamed)

    with pytest.raises(NotATimeLapse, match="renamed"):
        discover_time_lapse(renamed)


# --------------------------------------------------------------------------
# incomplete lapses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", MULTI_FILE_FORMATS)
def test_a_missing_point_is_a_gap_not_a_shift(tmp_path, fmt):
    record_lapse(tmp_path, 5, fmt=fmt, points=[0, 1, 3, 4])

    plan = _plan(next(tmp_path.glob("*_time0_*")))
    stack = LazyTimeLapseArray(plan)

    assert [slot.state for slot in plan.slots] == [
        TIMEPOINT_COMPLETE, TIMEPOINT_COMPLETE, TIMEPOINT_MISSING,
        TIMEPOINT_COMPLETE, TIMEPOINT_COMPLETE,
    ]
    assert plan.slots[2].reason == "not recorded"
    assert _values(stack) == [1, 2, 0, 4, 5]


def test_a_lapse_stopped_part_way_ends_at_its_last_point(tmp_path):
    record_lapse(tmp_path, 10, points=range(4))

    plan = _plan(next(tmp_path.glob("*_time00_*")))

    assert plan.shape[0] == 4
    assert plan.index.planned_count == 10


@pytest.mark.parametrize("fmt", ("hdf5", "zarr", "ome.tiff"))
def test_a_short_last_point_is_marked_and_padded(tmp_path, fmt):
    record_lapse(tmp_path, 3, fmt=fmt, camera=False, frames=6, points=[0, 1])
    record_point(tmp_path, fmt=fmt, index=2, total=3, camera=False, frames=6,
                 written=4)

    plan = _plan(next(tmp_path.glob("*_scan0_*")))
    stack = LazyTimeLapseArray(plan)

    last = plan.slots[-1]
    assert last.state == TIMEPOINT_INCOMPLETE
    assert last.reason == "stopped early: 4 of 6 frames"
    assert stack.shape == (3, 6, *FRAME_SHAPE)
    frames = stack[2]
    assert frames[:4].min() == 3 and frames[4:].max() == 0
    # The same padding holds for every way of slicing the frame axis.
    np.testing.assert_array_equal(stack[2, 3:5], frames[3:5])
    np.testing.assert_array_equal(stack[2, 5], frames[5])
    np.testing.assert_array_equal(stack[2, ::-1], frames[::-1])


def test_skipping_leaves_incomplete_points_off_the_end(tmp_path):
    record_lapse(tmp_path, 4, camera=False, frames=6, points=[0, 2])
    record_point(tmp_path, index=1, total=4, camera=False, frames=6, written=2)
    record_point(tmp_path, index=3, total=4, camera=False, frames=6, written=3)

    plan = _plan(next(tmp_path.glob("*_scan0_*")), incomplete=INCOMPLETE_SKIP)

    # Point 3 is gone; point 1 stays, marked, because removing it would move
    # point 2 onto point 1's time.
    assert [slot.lapse_index for slot in plan.slots] == [0, 1, 2]
    assert plan.slots[1].state == TIMEPOINT_INCOMPLETE
    assert [slot.lapse_index for slot in plan.skipped] == [3]


@pytest.mark.parametrize("fmt", MULTI_FILE_FORMATS)
def test_a_last_point_with_no_frames_is_marked_missing(tmp_path, fmt):
    record_lapse(tmp_path, 3, fmt=fmt, points=[0, 1])
    record_point(tmp_path, fmt=fmt, index=2, total=3, written=0)

    first = next(tmp_path.glob("*_time0_*"))
    marked = _plan(first)
    skipped = _plan(first, incomplete=INCOMPLETE_SKIP)

    assert marked.slots[2].state == TIMEPOINT_MISSING
    assert marked.shape[0] == 3
    assert skipped.shape[0] == 2


def test_a_point_that_changed_shape_is_refused_by_name(tmp_path):
    record_lapse(tmp_path, 4, points=[0, 1, 3])
    record_point(tmp_path, index=2, total=4, frame_shape=(8, 5))

    with pytest.raises(TimeLapseShapeError, match=r"point 2 is 1x8x5 but point 0 is 1x4x5"):
        _plan(next(tmp_path.glob("*_time0_*")))


# --------------------------------------------------------------------------
# laziness
# --------------------------------------------------------------------------


def test_nothing_is_read_until_the_stack_is_indexed(tmp_path, monkeypatch):
    paths = record_lapse(tmp_path, 6)
    reads = []
    real_read = lapse_source._ItemReader.read

    def counting(self, ref, dataset, key):
        reads.append(ref.ordinal)
        return real_read(self, ref, dataset, key)

    monkeypatch.setattr(lapse_source._ItemReader, "read", counting)

    stack = open_time_lapse(paths[2])
    assert reads == []

    stack[4]
    assert reads == [4]
    reads.clear()
    stack[1:3, :2]
    assert reads == [1, 2]
    reads.clear()
    stack.to_dask()[5].compute()
    assert reads == [5]


def test_browsing_a_long_lapse_holds_only_a_few_files_open(tmp_path, monkeypatch):
    paths = record_lapse(tmp_path, 12)
    stack = open_time_lapse(paths[0])
    reader = stack._reader

    _values(stack)

    assert len(reader._containers) <= 8
    stack.close()
    assert not reader._containers

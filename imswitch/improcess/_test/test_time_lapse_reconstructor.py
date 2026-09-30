"""The Time lapse reconstructor: options, the T axis, export.

The stacking itself is the data layer's and is pinned in
``test_time_lapse_source``; this covers what the reconstructor adds on top --
the picker entry, planned-versus-actual time, marking or skipping incomplete
points, the rows it publishes, and a streamed export that keeps both times.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from types import SimpleNamespace
from xml.etree import ElementTree

import h5py
import numpy as np
import pytest
import tifffile
import zarr

from imswitch.improcess._test._lapse_recordings import (
    FRAME_SHAPE,
    T0,
    record_lapse,
    record_point,
)
from imswitch.improcess.model import lapse_source
from imswitch.improcess.model.lapse_source import (
    TIME_LAPSE_SOURCE_KIND,
    LazyTimeLapseArray,
    discover_time_lapse,
)
from imswitch.improcess.reconstructors import (
    _AVAILABLE_RECONSTRUCTOR_CLASSES,
    available_reconstructor_ids,
)
from imswitch.improcess.reconstructors.base import (
    CancellationToken,
    ReconstructionCancelled,
    ReconstructionContext,
)
from imswitch.improcess.reconstructors.time_lapse import (
    TimeLapseReconstructor,
    TimeLapseResult,
)


def _item(path, dataset=None):
    """An opened item, as a plain image DataObj describes itself."""
    return SimpleNamespace(
        dataPath=str(path), name=path.name, datasetName=dataset,
        sourceKind="image",
    )


def _lapse_source(path):
    """The whole lapse, as the loader hands it over."""
    index = discover_time_lapse(path)
    return SimpleNamespace(
        dataPath=str(index.anchor.path), name=index.name,
        sourceKind=TIME_LAPSE_SOURCE_KIND, sourceMetadata=index,
    )


def _process(data_obj, **params):
    return TimeLapseReconstructor().process(data_obj, params)


def test_time_lapse_is_a_built_in_reconstructor():
    assert "time-lapse" in available_reconstructor_ids()
    assert _AVAILABLE_RECONSTRUCTOR_CLASSES["time-lapse"] is TimeLapseReconstructor
    reconstructor = TimeLapseReconstructor()
    assert reconstructor.make_metadata_dialog(None) is None
    assert reconstructor.execution_policy == "worker"
    assert TIME_LAPSE_SOURCE_KIND in reconstructor.accepted_source_kinds


@pytest.mark.parametrize("opened", ["item", "lapse"])
def test_the_result_is_a_lazy_stack_on_the_planned_interval(tmp_path, opened):
    paths = record_lapse(tmp_path, 5, interval_s=10.0)
    data_obj = _item(paths[2]) if opened == "item" else _lapse_source(paths[2])

    result = _process(data_obj)

    assert isinstance(result, TimeLapseResult)
    assert isinstance(result.data, LazyTimeLapseArray)
    assert result.data.shape == (5, *FRAME_SHAPE)
    assert result.axis_labels == ["T", "Y", "X"]
    assert result.axis_scales == [10.0, 0.2, 0.1]
    assert result.scale_unit == "um"
    assert result.times_s == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert result.name == "12h00m00s_rec Camera time lapse"
    assert result.table_records() == []


def test_a_late_point_is_kept_and_listed(tmp_path):
    """Both times survive, so a point that ran late shows rather than hides."""
    record_lapse(tmp_path, 4, interval_s=10.0, points=[0, 1, 3])
    record_point(tmp_path, index=2, total=4, interval_s=10.0,
                 started_at=T0 + timedelta(seconds=24.0))
    first = next(tmp_path.glob("*_time0_*"))

    planned = _process(_item(first), time_axis="planned")
    actual = _process(_item(first), time_axis="actual")

    assert planned.times_s == [0.0, 10.0, 20.0, 30.0]
    assert actual.times_s == [0.0, 10.0, 24.0, 30.0]
    assert actual.axis_scales[0] == pytest.approx(10.0)
    points = planned.metadata["time_lapse"]["points"]
    assert [point["late"] for point in points] == [False, False, True, False]
    assert points[2]["delay_s"] == pytest.approx(4.0)
    rows = planned.table_records()
    assert [(row["point"], row["note"]) for row in rows] == [(2, "started late")]


def test_a_scan_lapse_has_no_planned_interval_and_uses_actual_times(tmp_path):
    """A scan lapse waits a delay after each scan; it keeps no schedule."""
    paths = []
    for index, second in enumerate((0.0, 31.0, 65.0)):
        paths.append(record_point(
            tmp_path, index=index, total=3, camera=False, frames=2,
            layout_kind="time", started_at=T0 + timedelta(seconds=second),
        ))

    inspection = TimeLapseReconstructor().inspect_source(_item(paths[0]))
    result = _process(_item(paths[0]), time_axis="planned")

    assert inspection.metadata["planned_interval_available"] is False
    info = result.metadata["time_lapse"]
    assert info["time_axis"] == "actual"
    assert info["time_axis_requested"] == "planned"
    assert result.times_s == [0.0, 31.0, 65.0]
    assert result.axis_scales[0] == pytest.approx(32.5)


def test_a_missing_first_point_does_not_share_the_first_time(tmp_path):
    """Actual times count from the first point that has one."""
    for index, second in ((1, 30.0), (2, 61.0), (3, 90.0)):
        record_point(tmp_path, index=index, total=4, camera=False, frames=2,
                     layout_kind="time", started_at=T0 + timedelta(seconds=second))
    first = next(tmp_path.glob("*_scan1_*"))

    result = _process(_item(first), time_axis="actual")

    assert result.data.shape[0] == 4
    assert result.times_s == pytest.approx([0.0, 30.0, 61.0, 90.0])
    info = result.metadata["time_lapse"]
    assert info["actual_origin_estimated"] is True
    assert info["points"][0]["state"] == "missing"
    assert info["points"][0]["time_estimated"] is True


def test_incomplete_points_are_marked_or_skipped(tmp_path):
    record_lapse(tmp_path, 3, camera=False, frames=4, points=[0, 1])
    record_point(tmp_path, index=2, total=3, camera=False, frames=4, written=1)
    first = next(tmp_path.glob("*_scan0_*"))

    marked = _process(_item(first), incomplete="mark")
    skipped = _process(_item(first), incomplete="skip")

    assert marked.data.shape[0] == 3
    assert marked.name.endswith("(1 of 3 marked)")
    assert [(row["point"], row["state"], row["note"]) for row in marked.table_records()] == [
        (2, "incomplete", "stopped early: 1 of 4 frames")
    ]
    assert skipped.data.shape[0] == 2
    assert skipped.name.endswith("(1 skipped)")
    assert skipped.metadata["time_lapse"]["incomplete_points"] == "skip"


def test_a_detector_can_be_chosen(tmp_path):
    record_lapse(tmp_path, 3, detector="Camera")
    record_lapse(tmp_path, 3, detector="APD", frame_shape=(3, 3))
    first = next(tmp_path.glob("*_time0_Camera*"))

    inspection = TimeLapseReconstructor().inspect_source(_item(first))
    result = _process(_item(first), detector="APD")

    assert [choice.value for choice in inspection.choices["detector"]] == ["APD", "Camera"]
    assert [choice.metadata["default"] for choice in inspection.choices["detector"]] == [
        False, True
    ]
    assert result.data.shape == (3, 3, 3)


def test_inspection_says_why_a_file_is_not_a_lapse(tmp_path):
    single = record_point(tmp_path, index=0, total=1)

    inspection = TimeLapseReconstructor().inspect_source(_item(single))

    assert "single recording" in inspection.warning
    with pytest.raises(ValueError, match="single recording"):
        _process(_item(single))


def test_inspection_reports_a_stopped_lapse(tmp_path):
    paths = record_lapse(tmp_path, 10, points=range(3))

    inspection = TimeLapseReconstructor().inspect_source(_lapse_source(paths[0]))

    assert inspection.metadata["found"] == 3
    assert "3 of 10 planned points were recorded" in inspection.warning


def test_reconstructing_again_picks_up_points_recorded_since_opening(tmp_path):
    paths = record_lapse(tmp_path, 4, points=[0, 1])
    data_obj = _lapse_source(paths[0])
    record_lapse(tmp_path, 4, points=[2, 3])

    assert _process(data_obj).data.shape[0] == 4


def test_the_header_pass_reports_progress_and_can_be_cancelled(tmp_path):
    paths = record_lapse(tmp_path, 5)
    updates = []
    context = ReconstructionContext(progress_callback=updates.append)

    _process_with = TimeLapseReconstructor().process
    _process_with(_item(paths[0]), {}, context=context)

    phases = [update.phase for update in updates]
    assert phases[0] == "inspect" and phases[-1] == "finalize"
    assert any("5 of 5 timepoint headers" in update.message for update in updates)

    token = CancellationToken()
    token.cancel()
    with pytest.raises(ReconstructionCancelled):
        _process_with(
            _item(paths[0]), {},
            context=ReconstructionContext(cancellation_token=token),
        )


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


@pytest.fixture
def late_lapse(tmp_path):
    record_lapse(tmp_path / "lapse", 3, interval_s=10.0, points=[0, 2])
    record_point(tmp_path / "lapse", index=1, total=3, interval_s=10.0,
                 started_at=T0 + timedelta(seconds=13.0))
    return _item(next((tmp_path / "lapse").glob("*_time0_*")))


def _counting_reads(monkeypatch):
    reads = []
    real_read = lapse_source._ItemReader.read

    def counting(self, ref, dataset, key):
        reads.append(ref.ordinal)
        return real_read(self, ref, dataset, key)

    monkeypatch.setattr(lapse_source._ItemReader, "read", counting)
    return reads


def test_export_streams_one_timepoint_at_a_time_as_ome_tiff(
    late_lapse, tmp_path, monkeypatch
):
    result = _process(late_lapse, time_axis="actual")
    reads = _counting_reads(monkeypatch)
    path = tmp_path / "out.ome.tif"

    result.save(path)

    assert reads == [0, 1, 2]
    with tifffile.TiffFile(path) as handle:
        series = handle.series[0]
        assert series.axes == "TYX"
        assert series.asarray()[:, 0, 0].tolist() == [1, 2, 3]
        xml = handle.ome_metadata
    # The viewer's even spacing is the typical step; every plane keeps its own.
    assert 'TimeIncrement="10.0"' in xml
    assert re.findall(r'DeltaT="([^"]+)"', xml) == ["0.0", "13.0", "20.0"]
    description = json.loads(
        ElementTree.fromstring(xml).find(".//{*}Description").text
    )
    points = description["time_lapse"]["points"]
    assert [point["planned_s"] for point in points] == [0.0, 10.0, 20.0]
    assert [point["actual_s"] for point in points] == [0.0, 13.0, 20.0]
    assert description["time_lapse"]["time_axis"] == "actual"


@pytest.mark.parametrize("suffix", [".h5", ".ome.zarr"])
def test_export_to_hdf5_and_zarr_keeps_every_time(late_lapse, tmp_path, suffix):
    result = _process(late_lapse, time_axis="planned")
    path = tmp_path / f"out{suffix}"

    result.save(path)

    if suffix == ".h5":
        with h5py.File(path, "r") as handle:
            assert handle["data"].shape == (3, *FRAME_SHAPE)
            assert handle["data"].chunks == (1, *FRAME_SHAPE)
            assert handle["t_seconds"][...].tolist() == [0.0, 10.0, 20.0]
            assert "time_lapse" in handle.attrs
    else:
        root = zarr.open_group(str(path), mode="r")
        assert root["0"].shape == (3, *FRAME_SHAPE)
        assert root.attrs["t_seconds"] == [0.0, 10.0, 20.0]
        axes = root.attrs["ome"]["multiscales"][0]["axes"]
        assert [axis["name"] for axis in axes] == ["t", "y", "x"]


def test_a_missing_point_exports_as_a_blank_plane_at_its_own_time(tmp_path):
    record_lapse(tmp_path / "lapse", 4, interval_s=5.0, points=[0, 1, 3])
    result = _process(_item(next((tmp_path / "lapse").glob("*_time0_*"))))
    path = tmp_path / "out.ome.tif"

    result.save(path)

    with tifffile.TiffFile(path) as handle:
        planes = handle.series[0].asarray()
        xml = handle.ome_metadata
    assert planes[:, 0, 0].tolist() == [1, 2, 0, 4]
    assert re.findall(r'DeltaT="([^"]+)"', xml) == ["0.0", "5.0", "10.0", "15.0"]
    points = result.metadata["time_lapse"]["points"]
    assert points[2]["state"] == "missing"
    assert np.asarray(result.data[2]).max() == 0

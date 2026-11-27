"""M1 physical-reader parity: real on-disk layouts, gaps, scopes, selection.

Run with the real Qt/Zarr ImSwitch environment; the purely structural tests
also run without a GUI (the source classes themselves need full ImSwitch).
"""

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.improcess.execution.contracts import Contract, Incompatible
from imswitch.improcess.execution.disk_sources import (
    inspect_detector_names, make_incremental_disk_source,
)
from imswitch.improcess.execution.frame_inbox import FrameInbox
from imswitch.improcess.execution.reader import IncrementalReader
from imswitch.improcess.live.lapse_group_indices import (
    next_scan_index, scan_group_indices,
)
from imswitch.improcess.live.sources import Hdf5LapseSource, Hdf5MultiFileLapseSource


class _Recon:
    @classmethod
    def contract(cls, info, params):
        return Contract(info.frames_per_timepoint, 1, dict(params))


def _hdf5_dataset(parent, *, n=2, index=0, detector="CAM"):
    cam = parent.create_group(detector)
    dataset = cam.create_dataset(
        "data", data=np.full((n, 2, 3), index + 1, dtype="uint16"),
        chunks=(1, 2, 3),
    )
    dataset.attrs["recording:detector_name"] = detector
    dataset.attrs["recording:mode"] = "ScanLapse"
    dataset.attrs["recording:frames_per_stack"] = n
    dataset.attrs["recording:expected_frames"] = n
    dataset.attrs["recording:num_timepoints"] = 3
    dataset.attrs["writing"] = False


def _drain(reader, limit=80):
    emitted = []
    for _ in range(limit):
        reader.prefetch()
        item = reader.next_item()
        if item is None:
            continue
        emitted.append(item)
        reader.acknowledge()
        if reader.done:
            break
    assert reader.done, "incremental source failed to terminate"
    return emitted


def test_scan_group_indexing_is_physical_not_positional():
    assert scan_group_indices(["notes", "scan2", "scan0", "scan10"]) == (0, 2, 10)
    assert next_scan_index((0, 2, 10), 0, stop=3) == 2
    assert next_scan_index((0, 2, 10), 2, stop=3) is None
    assert next_scan_index((0, 2, 10), 2) == 10


def test_only_declared_missing_ranges_can_be_padded():
    inbox = FrameInbox(max_bytes=24)
    a = SimpleNamespace(start=0, end=2, data=np.full((2, 2, 3), 7, np.uint8))
    b = SimpleNamespace(start=4, end=6, data=np.full((2, 2, 3), 9, np.uint8))
    inbox.append([a])
    with pytest.raises(ValueError, match="Unannounced raw frame gap"):
        inbox.append([b])
    assert inbox.missing_ranges == () and inbox.available == 2
    inbox.append([b], allowed_gaps=[(2, 4)])
    assert inbox.missing_ranges == ((2, 4),)
    # Only original pixels occupy the incoming-byte budget; the gap is
    # materialized on demand, one processing chunk at a time.
    assert inbox.nbytes == 24  # two 2-frame blocks retained
    assert inbox.available == 6
    rows = [inbox.take(1).data for _ in range(6)]
    assert [int(x[0, 0, 0]) for x in rows] == [7, 7, 0, 0, 9, 9]
    assert inbox.available == 0 and inbox.nbytes == 0


def test_inbox_gap_does_not_allow_overlap_or_mask_bad_data():
    inbox = FrameInbox(max_bytes=100)
    a = SimpleNamespace(start=0, end=1, data=np.ones((1, 2, 2), np.uint8))
    inbox.append([a])
    with pytest.raises(ValueError, match="Unannounced"):
        inbox.append([SimpleNamespace(start=3, end=4, data=a.data)])
    with pytest.raises(ValueError, match="Overlapping"):
        inbox.append([SimpleNamespace(start=0, end=1, data=a.data)], allowed_gaps=[(1, 3)])
    with pytest.raises(ValueError, match="shape/dtype"):
        inbox.append([SimpleNamespace(start=1, end=2, data=np.ones((1, 3, 2), np.uint8))])
    assert inbox.available == 1


def test_hdf5_single_file_missing_scan_stays_missing(tmp_path):
    path = tmp_path / "single_lapse.hdf5"
    with h5py.File(path, "w", libver="latest") as root:
        _hdf5_dataset(root.create_group("scan0"), index=0)
        _hdf5_dataset(root.create_group("scan2"), index=2)
    assert inspect_detector_names(path) == ("CAM",)
    src = Hdf5LapseSource(detector_name="CAM")
    reader = IncrementalReader(src, _Recon, {}, name="missing", path=path)
    reader.open()
    assert reader.info.T == 3 and reader.info.S == (2,)
    try:
        items = _drain(reader)
        assert [i.kind for i in items] == ["begin", "push", "push", "push", "push", "finish"]
        frames = np.concatenate([i.frames.data for i in items if i.frames is not None])
        assert frames.shape == (6, 2, 3)
        np.testing.assert_array_equal(frames[:, 0, 0], [1, 1, 0, 0, 3, 3])
        assert reader.missing_ranges == ((2, 4),)
    finally:
        reader.close()


def test_hdf5_single_file_nonzero_scope_is_selected_by_scan_number(tmp_path):
    path = tmp_path / "scoped.h5"
    with h5py.File(path, "w", libver="latest") as root:
        _hdf5_dataset(root.create_group("scan0"), index=0)
        _hdf5_dataset(root.create_group("scan2"), index=2)
    source = Hdf5LapseSource(detector_name="CAM", timepoint_start=2, timepoint_stop=3)
    reader = IncrementalReader(source, _Recon, {}, name="scoped", path=path)
    reader.open()
    try:
        items = _drain(reader)
        pixels = np.concatenate([i.frames.data for i in items if i.frames is not None])
        assert pixels.shape[0] == 2
        assert np.all(pixels == 3)
        assert reader.missing_ranges == ()
    finally:
        reader.close()
    source = Hdf5LapseSource(detector_name="CAM", timepoint_start=1, timepoint_stop=2)
    with pytest.raises(ValueError, match="not available"):
        source.open(path)
    source.close()


def test_multi_file_lapse_preserves_absent_intermediate_timepoint(tmp_path):
    for i in (0, 2):
        with h5py.File(tmp_path / f"lapse_scan{i}_CAM.hdf5", "w", libver="latest") as root:
            _hdf5_dataset(root, index=i)
    path = tmp_path / "lapse_scan0_CAM.hdf5"
    source = Hdf5MultiFileLapseSource(path, detector_name="CAM")
    reader = IncrementalReader(source, _Recon, {}, name="multi", path=path)
    reader.open()
    try:
        items = _drain(reader)
        frames = np.concatenate([i.frames.data for i in items if i.frames is not None])
        np.testing.assert_array_equal(frames[:, 0, 0], [1, 1, 0, 0, 3, 3])
        assert reader.missing_ranges == ((2, 4),)
    finally:
        reader.close()


def test_multi_detector_requires_explicit_choice(tmp_path):
    path = tmp_path / "dual.hdf5"
    with h5py.File(path, "w", libver="latest") as root:
        _hdf5_dataset(root, index=0, detector="CAM1")
        _hdf5_dataset(root, index=0, detector="CAM2")
    assert inspect_detector_names(path) == ("CAM1", "CAM2")
    with pytest.raises(Incompatible, match="Ambiguous multi-detector"):
        make_incremental_disk_source(path)
    source, name = make_incremental_disk_source(path, detector_name="CAM2")
    assert name == "CAM2"
    with pytest.raises(Incompatible, match="not found"):
        make_incremental_disk_source(path, detector_name="UNKNOWN")
    source.close()


@pytest.mark.parametrize("suffix", [".hdf5", ".zarr"])
def test_two_detector_single_file_lapse_is_not_silently_selected(tmp_path, suffix):
    if suffix == ".zarr":
        zarr = pytest.importorskip("zarr")
        root = zarr.open_group(str(tmp_path / f"dual{suffix}"), mode="w")
        group = root.create_group("scan0")
        for name in ("CAM1", "CAM2"):
            cam = group.create_group(name)
            cam.create_array("data", data=np.ones((2, 2, 3), np.uint16))
    else:
        with h5py.File(tmp_path / f"dual{suffix}", "w") as root:
            group = root.create_group("scan0")
            _hdf5_dataset(group, detector="CAM1")
            _hdf5_dataset(group, detector="CAM2")
    with pytest.raises(Incompatible, match="Ambiguous multi-detector"):
        make_incremental_disk_source(tmp_path / f"dual{suffix}")


def test_pause_never_skips_a_timepoint_boundary_with_large_chunks():
    """A requested pause may split a configured processing chunk."""
    from types import SimpleNamespace

    from imswitch.improcess.execution.reader import IncrementalReader

    class Source:
        def __init__(self):
            self.data = np.arange(8 * 2 * 2, dtype=np.uint16).reshape(8, 2, 2)
            self.cursor = 0
            self.info = SimpleNamespace(
                frame_shape=(2, 2), dtype=np.uint16,
                attrs={"recording:mode": "ScanLapse"},
                frames_per_stack=4, planned_timepoints=2,
            )
        def open(self, _path):
            return self.info
        def poll(self):
            start = self.cursor
            self.cursor = 8
            return [SimpleNamespace(start=start, end=8, data=self.data[start:].copy())] if start < 8 else []
        def is_complete(self):
            return self.cursor == 8
        def close(self):
            return None

    class R:
        @classmethod
        def contract(cls, info, params):
            return Contract(1, 4, {})

    reader = IncrementalReader(Source(), R, {}, name="pause")
    reader.open()
    reader.prefetch()
    first = reader.next_item()
    assert (first.frames.start, first.frames.end) == (0, 1)
    reader.acknowledge()
    reader.request_pause(True)
    second = reader.next_item()
    assert (second.frames.start, second.frames.end) == (1, 4)
    reader.acknowledge()
    assert reader.next_item() is None
    reader.request_pause(False)
    third = reader.next_item()
    assert (third.frames.start, third.frames.end) == (4, 8)
    reader.acknowledge()
    assert reader.next_item().kind == "finish"
    reader.acknowledge()
    assert reader.done

"""Headless regression tests for the ImControl-compatible HDF5 replay protocol."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import h5py
import numpy as np
import pytest


spec = importlib.util.spec_from_file_location(
    "fake_hdf5_timelapse", Path(__file__).with_name("fake_hdf5_timelapse.py")
)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


@pytest.fixture
def paths(tmp_path):
    source = tmp_path / "source.hdf5"
    watch = tmp_path / "watched"
    watch.mkdir()
    with h5py.File(source, "w") as handle:
        handle.create_group("CAM").create_dataset(
            "data", data=np.arange(6 * 3 * 4, dtype=np.uint16).reshape(6, 3, 4)
        )
    return source, watch


def _run(source, watch, mode, *flags):
    return replay.main([
        str(source), str(watch), "--mode", mode, "--name", "rec",
        "--timepoints", "3", "--duration", "0.001",
        "--interval", "0.1", "--between-timepoints", "0",
        "--startup-delay", "0", *flags,
    ])


def test_multifile_only_exposes_completed_timepoints(paths, monkeypatch):
    source, watch = paths
    original = replay._replay_one
    seen = []

    def observe(output, *args, **kwargs):
        index = args[2]
        assert output.name.endswith(".hdf5.partial")
        assert output.with_suffix("").suffix == ".hdf5"
        public = Path(str(output).removesuffix(".partial"))
        assert not public.exists()
        assert Path(f"{output}.claim").exists()
        if index:
            assert (watch / f"rec_scan{index - 1}_CAM.hdf5").exists()
        original(output, *args, **kwargs)
        assert not public.exists(), "Do not publish while the writer is open"
        seen.append(index)

    monkeypatch.setattr(replay, "_replay_one", observe)
    results = _run(source, watch, "multi-file")
    assert seen == [0, 1, 2]
    assert results == [watch / f"rec_scan{i}_CAM.hdf5" for i in range(3)]
    assert not list(watch.glob("*.partial*"))
    for i, result in enumerate(results):
        with h5py.File(result) as root:
            data = root["CAM/data"]
            assert int(data.attrs["recording:lapse_index"]) == i
            assert data.attrs["recording:num_timepoints"] == 3
            assert data.attrs["writing"] == False
            assert root["CAM/stream_complete"][0] == 1
            assert int(root["CAM/frames_committed"][0]) == 6


def test_singlefile_publishes_only_at_operation_terminal(paths, monkeypatch):
    source, watch = paths
    original = replay._replay_one
    seen = []

    def observe(output, *args, **kwargs):
        assert output == watch / "rec_CAM.hdf5.partial"
        assert not (watch / "rec_CAM.hdf5").exists()
        assert Path(f"{output}.claim").exists()
        index = args[2]
        if index:
            with h5py.File(output, "r") as file:
                assert f"scan{index-1}/CAM/data" in file
        original(output, *args, **kwargs)
        assert not (watch / "rec_CAM.hdf5").exists()
        seen.append(index)

    monkeypatch.setattr(replay, "_replay_one", observe)
    assert _run(source, watch, "single-file") == [watch / "rec_CAM.hdf5"]
    assert seen == [0, 1, 2]
    assert not list(watch.glob("*.partial*"))
    with h5py.File(watch / "rec_CAM.hdf5", "r") as f:
        assert list(f) == ["scan0", "scan1", "scan2"]


def test_early_stop_preserves_existing_groups_and_planned_count(paths):
    source, watch = paths
    result = _run(source, watch, "single-file", "--stop-after", "2")
    assert result == [watch / "rec_CAM.hdf5"]
    with h5py.File(result[0]) as f:
        assert list(f) == ["scan0", "scan1"]
        for index in range(2):
            ds = f[f"scan{index}/CAM/data"]
            assert int(ds.attrs["recording:num_timepoints"]) == 3
            assert int(ds.attrs["recording:lapse_index"]) == index
    assert not list(watch.glob("*.partial*"))


def test_multifile_early_stop_publishes_only_finished_points(paths):
    source, watch = paths
    results = _run(source, watch, "multi-file", "--stop-after", "2")
    assert len(results) == 2
    assert all(p.exists() for p in results)
    assert not (watch / "rec_scan2_CAM.hdf5").exists()


def test_legacy_direct_write_remains_available(paths, monkeypatch):
    source, watch = paths
    original = replay._replay_one
    seen = []

    def observe(output, *args, **kwargs):
        assert output.suffix == ".hdf5"
        assert not Path(f"{output}.claim").exists()
        original(output, *args, **kwargs)
        seen.append(output.exists())

    monkeypatch.setattr(replay, "_replay_one", observe)
    results = _run(source, watch, "multi-file", "--direct-write")
    assert len(results) == 3
    assert seen == [True] * 3


def test_existing_stage_or_claim_cannot_be_overwritten(paths):
    source, watch = paths
    staged = watch / "rec_CAM.hdf5.partial"
    staged.write_bytes(b"recoverable")
    with pytest.raises(SystemExit):
        _run(source, watch, "single-file")
    assert staged.read_bytes() == b"recoverable"
    staged.unlink()
    claim = watch / "rec_CAM.hdf5.partial.claim"
    claim.touch()
    with pytest.raises(SystemExit):
        _run(source, watch, "single-file")
    assert claim.exists()


def test_failed_publication_retains_recoverable_stage_and_claim(paths, monkeypatch):
    source, watch = paths

    def fail(*_args, **_kwargs):
        raise PermissionError("simulated Windows rename failure")

    monkeypatch.setattr(replay, "_publish_finalized", fail)
    with pytest.raises(PermissionError, match="simulated"):
        _run(source, watch, "single-file")
    stage = watch / "rec_CAM.hdf5.partial"
    assert stage.exists()
    assert (watch / "rec_CAM.hdf5.partial.claim").exists()
    assert not (watch / "rec_CAM.hdf5").exists()
    with h5py.File(stage) as file:
        assert list(file) == ["scan0", "scan1", "scan2"]


def test_deprecated_atomic_flag_uses_same_directory_stage(paths):
    source, watch = paths
    _run(source, watch, "multi-file", "--atomic-publish")
    assert len(list(watch.glob("*.hdf5"))) == 3
    assert not list(watch.glob("*.partial*"))

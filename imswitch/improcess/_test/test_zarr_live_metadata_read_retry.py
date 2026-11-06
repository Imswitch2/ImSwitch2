"""Live Zarr v3 metadata reading must tolerate a concurrent writer on Windows."""

import asyncio

import numpy as np
import pytest
import zarr

from imswitch.improcess.live.sources import (
    ZarrLapseSource,
    _RetryingReadLocalStore,
)


@pytest.mark.skipif(
    not hasattr(zarr.storage, "LocalStore"), reason="Zarr v3 LocalStore only"
)
def test_local_store_retries_transient_metadata_permission_error(tmp_path, monkeypatch):
    root = zarr.open_group(str(tmp_path / "recording.zarr"), mode="w")
    root.create_group("scan0")

    original_get = zarr.storage.LocalStore.get
    attempts = {"count": 0}

    async def intermittent_get(self, key, prototype, byte_range=None):
        if str(key) == "scan0/zarr.json" and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError(13, "temporarily locked", key)
        return await original_get(self, key, prototype, byte_range=byte_range)

    monkeypatch.setattr(zarr.storage.LocalStore, "get", intermittent_get)
    store = _RetryingReadLocalStore(str(tmp_path / "recording.zarr"), read_only=True)
    node = zarr.open_group(store, mode="r")["scan0"]
    assert node is not None
    assert attempts["count"] == 2


@pytest.mark.skipif(
    not hasattr(zarr.storage, "LocalStore"), reason="Zarr v3 LocalStore only"
)
def test_local_store_does_not_retry_non_metadata_permission_error(tmp_path, monkeypatch):
    store = _RetryingReadLocalStore(str(tmp_path), read_only=True)
    original_get = zarr.storage.LocalStore.get
    keys = []

    async def inaccessible_get(self, key, prototype, byte_range=None):
        keys.append(str(key))
        if str(key) == "unreadable/chunk":
            raise PermissionError(13, "access denied", key)
        return await original_get(self, key, prototype, byte_range=byte_range)

    monkeypatch.setattr(zarr.storage.LocalStore, "get", inaccessible_get)
    with pytest.raises(PermissionError):
        asyncio.run(store.get("unreadable/chunk", None))
    assert keys == ["unreadable/chunk"]


@pytest.mark.skipif(
    not hasattr(zarr.storage, "LocalStore"), reason="Zarr v3 LocalStore only"
)
def test_lapse_poll_survives_locked_data_metadata_without_skipping_frames(tmp_path, monkeypatch):
    path = tmp_path / "lapse.zarr"
    root = zarr.open_group(str(path), mode="w")
    det = root.create_group("scan0").create_group("CAM")
    expected = np.arange(3 * 4 * 4, dtype="uint16").reshape(3, 4, 4)
    array = det.create_array("data", data=expected, chunks=(1, 4, 4))
    array.attrs.update({
        "writing": True,
        "recording:frames_committed": 3,
        "recording:frames_per_stack": 3,
        "recording:num_timepoints": 1,
    })

    source = ZarrLapseSource(detector_name="CAM", chunk_size=1)
    source.open(path)
    original_get = zarr.storage.LocalStore.get
    attempts = {"count": 0}

    async def intermittent_get(self, key, prototype, byte_range=None):
        if str(key) == "scan0/CAM/data/zarr.json" and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError(13, "temporarily locked", key)
        return await original_get(self, key, prototype, byte_range=byte_range)

    monkeypatch.setattr(zarr.storage.LocalStore, "get", intermittent_get)
    try:
        chunks = source.poll()
        assert attempts["count"] == 2
        assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 1), (1, 2), (2, 3)]
        np.testing.assert_array_equal(np.concatenate([c.data for c in chunks]), expected)
        assert source.poll() == []
    finally:
        source.close()


@pytest.mark.skipif(
    not hasattr(zarr.storage, "LocalStore"), reason="Zarr v3 LocalStore only"
)
def test_persistent_metadata_permission_error_propagates(tmp_path, monkeypatch):
    store = _RetryingReadLocalStore(str(tmp_path), read_only=True)
    calls = []

    async def always_denied(self, key, prototype, byte_range=None):
        calls.append(key)
        raise PermissionError(13, "Access denied", key)

    monkeypatch.setattr(zarr.storage.LocalStore, "get", always_denied)
    monkeypatch.setattr(_RetryingReadLocalStore, "_READ_ATTEMPTS", 3)
    monkeypatch.setattr(_RetryingReadLocalStore, "_RETRY_DELAY_S", 0)
    with pytest.raises(PermissionError):
        asyncio.run(store.get("scan0/CAM/data/zarr.json", None))
    assert len(calls) == 3

"""ZarrStorer's store retries a write that Windows refuses because another
process (e.g. ImProcess reading the recording live) has zarr.json open."""
import os
import sys
import threading

import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the rename race exists only on Windows")


def _array_with_reader_holding_zarr_json(tmp_path, store):
    root = zarr.open_group(store, mode="w")
    data = root.create_array("data", shape=(0, 4, 4), chunks=(1, 4, 4), dtype="u2")
    reader = open(os.path.join(tmp_path, "data", "zarr.json"), "rb")
    return data, reader


@windows_only
def test_a_plain_local_store_fails_while_a_reader_holds_zarr_json(tmp_path):
    data, reader = _array_with_reader_holding_zarr_json(tmp_path, zarr.storage.LocalStore(str(tmp_path)))
    try:
        with pytest.raises(PermissionError):
            data.attrs["recording:frames_committed"] = 1
    finally:
        reader.close()


@windows_only
def test_zarr_storer_store_waits_for_the_reader_and_writes(tmp_path):
    data, reader = _array_with_reader_holding_zarr_json(tmp_path, ZarrStorer._make_store(str(tmp_path)))
    threading.Timer(0.05, reader.close).start()
    data.attrs["recording:frames_committed"] = 1
    assert zarr.open_array(str(tmp_path / "data"), mode="r").attrs["recording:frames_committed"] == 1


@windows_only
def test_zarr_storer_store_still_raises_when_the_file_stays_locked(tmp_path):
    data, reader = _array_with_reader_holding_zarr_json(tmp_path, ZarrStorer._make_store(str(tmp_path)))
    try:
        with pytest.raises(PermissionError):
            data.attrs["recording:frames_committed"] = 1
    finally:
        reader.close()

"""Phase 3: OME-NGFF (Zarr) storer -- multiscales metadata, flat chunk keys,
and that the legacy ``data`` array name (reader contract) is preserved."""
import json
import os

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import SaveMode, ZarrStorer
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN, MODE_SNAP, MODE_TIMELAPSE, build_ome_image_meta,
)


class _StubDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.2, 0.1]


class _StubDetectorManager:
    def __getitem__(self, name):
        return _StubDetector()


@pytest.fixture
def detman():
    return _StubDetectorManager()


def _multiscales(group):
    return dict(group.attrs)['ome']['multiscales'][0]


def _zarr_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def test_snap_writes_ngff_multiscales(detman, tmp_path):
    storer = ZarrStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img})

    zp = str(tmp_path / 'snap.zarr')
    g = zarr.open(zp, mode='r')['Cam']
    # standard OME-NGFF: multiscales pointing at the (preserved) data array
    ms = _multiscales(g)
    assert [a['name'] for a in ms['axes']] == ['t', 'y', 'x']      # 2D snap padded to 3D
    assert ms['datasets'][0]['path'] == 'data'
    assert ms['datasets'][0]['coordinateTransformations'][0]['scale'] == [1.0, 0.2, 0.1]
    assert _zarr_json(os.path.join(zp, 'Cam', 'data', 'zarr.json'))['dimension_names'] == [
        't', 'y', 'x']
    assert dict(zarr.open(zp, mode='r').attrs)['ome']['series'] == [{'path': 'Cam'}]
    # legacy reader contract: the image array is still named 'data', shape (1,Y,X)
    assert 'data' in g and g['data'].shape == (1, 48, 32)
    np.testing.assert_array_equal(np.asarray(g['data'])[0], img)


def test_streaming_ngff_and_flat_chunk_keys(detman, tmp_path):
    path = str(tmp_path / 'rec.zarr')
    storer = ZarrStorer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_TIMELAPSE, 2, pixel_size_yx_um=(0.2, 0.1),
        t_interval_s=0.05, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (48, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    for _ in range(4):
        storer.writeFrames('Cam', np.random.randint(1, 4096, (8, 48, 32), np.uint16))
    storer.finalizeStream({'Cam': 32}, {'Cam': path}, None, SaveMode.Disk)

    g = zarr.open(path, mode='r')['Cam']
    ms = _multiscales(g)
    assert [a['name'] for a in ms['axes']] == ['t', 'y', 'x']
    assert ms['datasets'][0]['coordinateTransformations'][0]['scale'] == [0.05, 0.2, 0.1]
    assert g['data'].shape == (32, 48, 32)
    assert _zarr_json(os.path.join(path, 'Cam', 'data', 'zarr.json'))['dimension_names'] == [
        't', 'y', 'x']

    # flat v2-style chunk keys (0.0.0), not nested v3 c/0/0/0 folders
    data_dir = os.path.join(path, 'Cam', 'data')
    chunk_keys = [os.path.relpath(os.path.join(dp, f), data_dir)
                  for dp, _, fs in os.walk(data_dir) for f in fs if f != 'zarr.json']
    assert chunk_keys, 'expected chunk files'
    assert not any(k.startswith('c' + os.sep) for k in chunk_keys), chunk_keys
    assert all('.' in os.path.basename(k) for k in chunk_keys), chunk_keys


def test_zstack_ngff_dimension_names_and_scale(detman, tmp_path):
    path = str(tmp_path / 'z.zarr')
    storer = ZarrStorer(str(tmp_path / 'z'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SCAN, 4, scan_dims=(32, 32, 4),
        pixel_size_yx_um=(0.2, 0.1), z_step_um=0.7, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (48, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    storer.writeFrames('Cam', np.random.randint(1, 4096, (4, 48, 32), np.uint16))
    storer.finalizeStream({'Cam': 4}, {'Cam': path}, None, SaveMode.Disk)

    g = zarr.open(path, mode='r')['Cam']
    ms = _multiscales(g)
    assert [a['name'] for a in ms['axes']] == ['z', 'y', 'x']
    assert ms['datasets'][0]['coordinateTransformations'][0]['scale'] == [0.7, 0.2, 0.1]
    assert g['data'].attrs['axes'] == ['Z', 'Y', 'X']
    assert _zarr_json(os.path.join(path, 'Cam', 'data', 'zarr.json'))['dimension_names'] == [
        'z', 'y', 'x']

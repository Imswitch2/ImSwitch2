"""Phase 2: OME-TIFF storer round-trip (snap + streaming), independent of the
full recording pipeline."""
import os
import tempfile

import numpy as np
import pytest
import tifffile

from imswitch.imcontrol.model.managers.RecordingManager import TiffStorer
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN, MODE_SCAN_LAPSE, MODE_SNAP, MODE_TIMELAPSE, build_ome_image_meta,
)


class _StubDetector:
    def __init__(self, dtype=np.uint16, pixelSizeUm=(1.0, 0.2, 0.1)):
        self.dtype = np.dtype(dtype)
        self.pixelSizeUm = list(pixelSizeUm)


class _StubDetectorManager:
    def __init__(self, det):
        self._det = det

    def __getitem__(self, name):
        return self._det


@pytest.fixture
def detman():
    return _StubDetectorManager(_StubDetector())


def test_snap_writes_ome_tiff_yx(detman, tmp_path):
    storer = TiffStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img})
    f = str(tmp_path / 'snap_Cam.ome.tiff')
    with tifffile.TiffFile(f) as t:
        assert t.is_ome
        assert t.series[0].axes == 'YX'
        xml = t.ome_metadata or ''
        assert 'PhysicalSizeX="0.1"' in xml
        np.testing.assert_array_equal(t.asarray(), img)


def test_streaming_injects_ome_xml_tyx(detman, tmp_path):
    path = str(tmp_path / 'rec_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_TIMELAPSE, 2, pixel_size_yx_um=(0.2, 0.1),
        t_interval_s=0.05, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (48, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False, saveMode=None)
    written = []
    for _ in range(5):
        batch = np.random.randint(1, 4096, (8, 48, 32), np.uint16)
        written.append(batch)
        storer.writeFrames('Cam', batch)
    storer.finalizeStream({'Cam': 40}, {'Cam': path}, None, None)

    with tifffile.TiffFile(path) as t:
        assert t.is_ome
        assert t.series[0].axes == 'TYX'
        assert t.series[0].shape == (40, 48, 32)
        xml = t.ome_metadata or ''
        assert 'SizeT="40"' in xml
        np.testing.assert_array_equal(t.asarray(), np.concatenate(written))


def test_streaming_zstack_labels_zyx(detman, tmp_path):
    path = str(tmp_path / 'z_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'z'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SCAN, 5, scan_dims=(32, 32, 5),  # Nz=5 -> Z stack
        pixel_size_yx_um=(0.2, 0.1), z_step_um=0.5, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (32, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False, saveMode=None)
    storer.writeFrames('Cam', np.random.randint(1, 4096, (5, 32, 32), np.uint16))
    storer.finalizeStream({'Cam': 5}, {'Cam': path}, None, None)
    with tifffile.TiffFile(path) as t:
        assert t.is_ome and t.series[0].axes == 'ZYX'
        assert 'PhysicalSizeZ="0.5"' in (t.ome_metadata or '')


def test_scan_lapse_zstack_uses_per_scan_zyx(detman, tmp_path):
    path = str(tmp_path / 'scan_lapse_Cam.ome.tiff')
    storer = TiffStorer(str(tmp_path / 'scan_lapse'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SCAN_LAPSE, 5, scan_dims=(32, 32, 5),
        pixel_size_yx_um=(0.2, 0.1), z_step_um=0.7, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (32, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=True, saveMode=None)
    storer.writeFrames('Cam', np.random.randint(1, 4096, (5, 32, 32), np.uint16))
    storer.finalizeStream({'Cam': 5}, {'Cam': path}, None, None)
    with tifffile.TiffFile(path) as t:
        assert t.is_ome
        assert t.series[0].axes == 'ZYX'
        assert t.series[0].shape == (5, 32, 32)
        assert 'SizeZ="5"' in (t.ome_metadata or '')
        assert 'SizeT="1"' in (t.ome_metadata or '')


def test_abort_removes_partial_file(detman, tmp_path):
    path = str(tmp_path / 'a_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'a'), detman)
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (16, 16)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False, saveMode=None)
    storer.writeFrames('Cam', np.zeros((3, 16, 16), np.uint16))
    storer.abortStream({'Cam': path}, {'Cam': path}, None)
    assert not os.path.exists(path)

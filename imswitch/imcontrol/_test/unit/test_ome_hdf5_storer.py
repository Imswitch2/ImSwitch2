"""Phase 4: HDF5 storer embeds OME-XML (alongside Fiji element_size_um), with
the structured /det/data layout (reader contract) preserved."""
import re

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, SaveMode
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


def test_snap_embeds_ome_xml_and_keeps_fiji(detman, tmp_path):
    storer = HDF5Storer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img})

    with h5py.File(str(tmp_path / 'snap_Cam.h5'), 'r') as h:
        g = h['Cam']
        xml = g.attrs['ome_xml']
        assert '<OME' in xml and 'DimensionOrder' in xml
        assert 'PhysicalSizeX="0.1"' in xml
        # Fiji pixel-size convention preserved on the data dataset
        assert list(g['data'].attrs['element_size_um']) == [1.0, 0.2, 0.1]
        # legacy reader contract: structured /det/data, (1,Y,X)
        assert g['data'].shape == (1, 48, 32)
        np.testing.assert_array_equal(g['data'][0], img)


def test_streaming_embeds_ome_xml_at_finalize(detman, tmp_path):
    path = str(tmp_path / 'rec.hdf5')
    storer = HDF5Storer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_TIMELAPSE, 2, pixel_size_yx_um=(0.2, 0.1),
        t_interval_s=0.05, dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (48, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    for _ in range(4):
        storer.writeFrames('Cam', np.random.randint(1, 4096, (8, 48, 32), np.uint16))
    storer.finalizeStream({'Cam': 32}, {'Cam': path}, None, SaveMode.Disk)

    with h5py.File(path, 'r') as h:
        g = h['Cam']
        xml = g.attrs['ome_xml']
        assert re.search(r'SizeT="32"', xml)
        assert 'PhysicalSizeX="0.1"' in xml
        assert g['data'].shape == (32, 48, 32)
        assert g['data'].attrs['writing'] in (False, np.False_)


def test_streaming_linestep_tcyx_keeps_ome_xml(detman, tmp_path):
    """SizeC > 1 (retained line-steps) with the default one-entry channel
    list used to raise inside build_ome_xml; HDF5's guarded embed then
    silently omitted ome_xml while pixels and axes survived."""
    path = str(tmp_path / 'ls.hdf5')
    storer = HDF5Storer(str(tmp_path / 'ls'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SCAN, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (2, 4, 5)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    storer.writeFrames('Cam', np.zeros((1, 2, 4, 5), np.uint16))
    storer.finalizeStream({'Cam': 1}, {'Cam': path}, None, SaveMode.Disk)

    with h5py.File(path, 'r') as h:
        g = h['Cam']
        assert g['data'].shape == (1, 2, 4, 5)
        assert g['data'].attrs['axes'] == 'TCYX'
        xml = g.attrs['ome_xml']
        assert 'SizeC="2"' in xml
        assert xml.count('Name="Cam"') == 2

"""Scan-axis provenance survives every recording container.

``ScanStage:scan_axis_devices`` / ``ScanStage:scan_axis_physical`` are
write-only shared attributes (see ``scan_axis_provenance``): recordings keep
compatibility ``YX`` axes even for a single-axis scan, so these fields are the
only place a stored file says *which physical axis was actually scanned*.
HDF5 and Zarr serialize shared attributes natively; OME-TIFF's counterpart is
an OME ``MapAnnotation`` embedded in the OME-XML (``build_ome_xml``) — its
``snap()``/``openStream()`` used to ignore ``attrs`` entirely, silently
dropping the provenance from TIFF recordings.
"""
import json
import xml.etree.ElementTree as ET

import h5py
import numpy as np
import pytest
import tifffile
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer, SaveMode, TiffStorer, ZarrStorer,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN, MODE_SNAP, build_ome_image_meta,
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


#: What updateScanStageAttrs writes for a Z-piezo-only scan, flattened the way
#: RecordingManager hands attrs to storers ('Category:key' per detector).
PROVENANCE_ATTRS = {
    'ScanStage:scan_axis_devices': ['ND-PiezoZ'],
    'ScanStage:scan_axis_physical': ['Z'],
}


def _decoded(values):
    return [v.decode() if isinstance(v, bytes) else str(v) for v in values]


def _tiff_map_annotations(path):
    """Parse the embedded OME-XML's ImSwitch MapAnnotation into a dict."""
    with tifffile.TiffFile(path) as t:
        assert t.is_ome
        xml = t.ome_metadata or ''
    root = ET.fromstring(xml)
    ns = root.tag.partition('}')[0].lstrip('{')
    annotations = {}
    for m in root.iter(f'{{{ns}}}M'):
        annotations[m.attrib['K']] = m.text
    # Structural validity: the image references the annotation.
    refs = [el.attrib['ID'] for el in root.iter(f'{{{ns}}}AnnotationRef')]
    assert 'Annotation:ImSwitch:0' in refs
    return annotations


def _assert_tiff_provenance(path):
    annotations = _tiff_map_annotations(path)
    assert json.loads(annotations['ScanStage:scan_axis_devices']) == ['ND-PiezoZ']
    assert json.loads(annotations['ScanStage:scan_axis_physical']) == ['Z']


def _scan_meta():
    # A Z-only stepped scan records as one (1, N) line; N=20 pixels here.
    return build_ome_image_meta(
        'Cam', MODE_SCAN, 1, scan_dims=(20, 1),
        pixel_size_yx_um=(0.5, 0.5), dtype=np.uint16)


def test_tiff_snap_roundtrips_scan_axis_provenance(detman, tmp_path):
    storer = TiffStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img}, attrs={'Cam': dict(PROVENANCE_ATTRS)})

    path = str(tmp_path / 'snap_Cam.ome.tiff')
    _assert_tiff_provenance(path)
    # The rewritten OME-XML still describes the image correctly.
    with tifffile.TiffFile(path) as t:
        assert t.series[0].axes == 'YX'
        assert 'PhysicalSizeX="0.1"' in (t.ome_metadata or '')
        np.testing.assert_array_equal(t.asarray(), img)


def test_tiff_snap_without_attrs_stays_untouched(detman, tmp_path):
    storer = TiffStorer(str(tmp_path / 'plain'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    storer.snap({'Cam': np.zeros((8, 8), np.uint16)})
    with tifffile.TiffFile(str(tmp_path / 'plain_Cam.ome.tiff')) as t:
        assert 'MapAnnotation' not in (t.ome_metadata or '')


def test_tiff_stream_roundtrips_scan_axis_provenance(detman, tmp_path):
    path = str(tmp_path / 'rec_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': _scan_meta()}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (1, 20)},
                      {'Cam': dict(PROVENANCE_ATTRS)},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=None)
    frame = np.random.randint(1, 4096, (1, 1, 20), np.uint16)
    storer.writeFrames('Cam', frame)
    storer.finalizeStream({'Cam': 1}, {'Cam': path}, None, None)

    _assert_tiff_provenance(path)
    with tifffile.TiffFile(path) as t:
        np.testing.assert_array_equal(
            np.asarray(t.asarray()).reshape(1, 20), frame[0])


def test_hdf5_snap_and_stream_roundtrip_scan_axis_provenance(detman, tmp_path):
    storer = HDF5Storer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': _scan_meta()}
    storer.snap({'Cam': np.zeros((1, 20), np.uint16)},
                attrs={'Cam': dict(PROVENANCE_ATTRS)})
    with h5py.File(str(tmp_path / 'snap_Cam.h5'), 'r') as h:
        stage = h['Cam/metadata/ScanStage']
        assert _decoded(stage.attrs['scan_axis_devices']) == ['ND-PiezoZ']
        assert _decoded(stage.attrs['scan_axis_physical']) == ['Z']

    path = str(tmp_path / 'rec.hdf5')
    storer = HDF5Storer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': _scan_meta()}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (1, 20)},
                      {'Cam': dict(PROVENANCE_ATTRS)},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    storer.writeFrames('Cam', np.zeros((1, 1, 20), np.uint16))
    storer.finalizeStream({'Cam': 1}, {'Cam': path}, None, SaveMode.Disk)
    with h5py.File(path, 'r') as h:
        stage = h['Cam/metadata/ScanStage']
        assert _decoded(stage.attrs['scan_axis_devices']) == ['ND-PiezoZ']
        assert _decoded(stage.attrs['scan_axis_physical']) == ['Z']


def test_zarr_snap_and_stream_roundtrip_scan_axis_provenance(detman, tmp_path):
    storer = ZarrStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': _scan_meta()}
    storer.snap({'Cam': np.zeros((1, 20), np.uint16)},
                attrs={'Cam': dict(PROVENANCE_ATTRS)})
    stage = zarr.open(str(tmp_path / 'snap.zarr'), mode='r')['Cam/metadata/ScanStage']
    assert list(stage.attrs['scan_axis_devices']) == ['ND-PiezoZ']
    assert list(stage.attrs['scan_axis_physical']) == ['Z']

    path = str(tmp_path / 'rec.zarr')
    storer = ZarrStorer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': _scan_meta()}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (1, 20)},
                      {'Cam': dict(PROVENANCE_ATTRS)},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=SaveMode.Disk)
    storer.writeFrames('Cam', np.zeros((1, 1, 20), np.uint16))
    storer.finalizeStream({'Cam': 1}, {'Cam': path}, None, SaveMode.Disk)
    stage = zarr.open(path, mode='r')['Cam/metadata/ScanStage']
    assert list(stage.attrs['scan_axis_devices']) == ['ND-PiezoZ']
    assert list(stage.attrs['scan_axis_physical']) == ['Z']

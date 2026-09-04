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


def test_3d_tile_writes_one_stage_position_per_plane(tmp_path):
    """OME records stage position per plane and tifffile indexes that list per
    plane, so a Z stack whose position lists held a single entry raised
    ``IndexError: list index out of range for attribute 'PositionX'`` and no
    tile was saved at all. n_planes() returned a hardcoded 1 regardless of the
    data -- correct for a single-plane snap, wrong for every 3D tile.
    """
    import numpy as np
    import tifffile

    from imswitch.imcontrol.model.managers import recording_metadata as _ome

    meta = _ome.build_ome_image_meta(
        'Tile', _ome.MODE_SCAN, 5, scan_dims=(1, 1, 5), z_step_um=0.5,
        pixel_size_yx_um=(0.1, 0.1), dtype=np.uint16,
        stage_position_um=(100.0, 200.0, 40.0),
    )
    image = np.zeros((5, 64, 64), np.uint16)

    assert meta.n_planes(image.shape) == 5
    md = meta.tiff_metadata(image.shape)
    assert len(md['Plane']['PositionX']) == 5
    assert len(md['Plane']['PositionZ']) == 5

    path = tmp_path / 'tile.ome.tiff'
    tifffile.imwrite(str(path), image, ome=True, bigtiff=True, metadata=md)

    with tifffile.TiffFile(str(path)) as handle:
        xml = handle.ome_metadata
        assert handle.series[0].shape == (5, 64, 64)
    assert 'SizeZ="5"' in xml
    assert xml.count('PositionX=') == 5


def test_a_plain_2d_snap_still_gets_exactly_one_position():
    import numpy as np

    from imswitch.imcontrol.model.managers import recording_metadata as _ome

    meta = _ome.build_ome_image_meta(
        'Snap', _ome.MODE_SNAP, 1, pixel_size_yx_um=(0.1, 0.1),
        dtype=np.uint16, stage_position_um=(1.0, 2.0, 3.0),
    )

    md = meta.tiff_metadata((64, 64))

    assert len(md['Plane']['PositionX']) == 1


def test_a_recording_without_a_stage_position_is_unchanged():
    import numpy as np

    from imswitch.imcontrol.model.managers import recording_metadata as _ome

    meta = _ome.build_ome_image_meta(
        'Cam', _ome.MODE_TIMELAPSE, 10, pixel_size_yx_um=(0.1, 0.1),
        dtype=np.uint16,
    )

    assert 'Plane' not in meta.tiff_metadata((10, 64, 64))


def test_a_single_frame_tiff_records_the_layout_of_what_it_stored(detman, tmp_path):
    """The storer drops the frame axis, so the layout it embeds must too.

    A scan-driven detector emits one assembled frame, and a one-frame OME-TIFF
    stores the plane itself rather than a one-element stack. The layout still
    declared the ``frame`` axis, so the container had one axis fewer than the
    recorded layout claimed -- and an explicit layout that disagrees with its
    container is refused outright by design, never fallen back from. The
    recording was therefore unreadable rather than merely unannotated.
    """
    import tifffile as tiff

    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_ASSEMBLED_IMAGE,
        AcquisitionLayout,
        AcquisitionLoop,
        decode_acquisition_layout,
        encode_acquisition_layout,
    )

    rows, cols, conditions = 2, 3, 2
    loops = (
        AcquisitionLoop('scan_y', 'scan_y', rows, storage_axis='scan_y'),
        AcquisitionLoop(
            'condition', 'condition', conditions, storage_axis='condition'
        ),
        AcquisitionLoop('scan_x', 'scan_x', cols, storage_axis='scan_x'),
    )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
        detector='Cam',
        storage_axes=('frame', 'condition', 'scan_y', 'scan_x'),
        event_loops=loops,
        provenance='recorded',
    )

    path = str(tmp_path / 'assembled_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'assembled'), detman)
    # A scan-driven detector's frame carries the line-step axis, so its OME
    # metadata describes three axes and the storer stores the plane itself.
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16
    ).padded_to(3)}
    attrs = {'Cam': {
        'AcquisitionLayout:schema': ACQUISITION_LAYOUT_SCHEMA,
        'AcquisitionLayout:json': encode_acquisition_layout(layout),
    }}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (rows, cols)}, attrs,
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=None)
    storer.writeFrames(
        'Cam', np.ones((1, conditions, rows, cols), np.uint16)
    )
    storer.finalizeStream({'Cam': 1}, {'Cam': path}, None, None)

    with tiff.TiffFile(path) as file:
        embedded = file.ome_metadata or ''
    assert 'AcquisitionLayout:json' in embedded

    stored = storer.omeMeta['Cam'].annotations['AcquisitionLayout:json']
    recovered = decode_acquisition_layout(stored)
    assert recovered.storage_axes == ('condition', 'scan_y', 'scan_x')
    # Only the axis the storer actually dropped: the loops are untouched.
    assert recovered.event_loops == layout.event_loops


def test_a_multi_frame_tiff_keeps_its_frame_axis(detman, tmp_path):
    """The collapse must follow the storer, not fire on every recording."""
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
        decode_acquisition_layout,
        encode_acquisition_layout,
    )

    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Cam',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=(AcquisitionLoop('scan_x', 'scan_x', 4),),
        provenance='recorded',
    )
    path = str(tmp_path / 'stream_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'stream'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_TIMELAPSE, 4, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16)}
    storer.openStream(
        {'Cam': path}, ['Cam'], {'Cam': (8, 8)},
        {'Cam': {
            'AcquisitionLayout:schema': ACQUISITION_LAYOUT_SCHEMA,
            'AcquisitionLayout:json': encode_acquisition_layout(layout),
        }},
        singleMultiDetectorFile=False, singleLapseFile=False, saveMode=None)
    storer.writeFrames('Cam', np.ones((4, 8, 8), np.uint16))
    storer.finalizeStream({'Cam': 4}, {'Cam': path}, None, None)

    stored = storer.omeMeta['Cam'].annotations['AcquisitionLayout:json']
    assert decode_acquisition_layout(stored).storage_axes == (
        'frame', 'detector_y', 'detector_x'
    )


def test_a_deduplicated_ome_tiff_keeps_its_two_part_suffix(tmp_path):
    """``rec_1.ome.tiff``, not ``rec.ome_1.tiff``.

    ``os.path.splitext`` splits on the last dot only, so the counter landed
    inside the suffix: the file sorted away from its siblings and matched no
    ``*.ome.tiff`` glob anyone would write.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager

    manager = RecordingManager.__new__(RecordingManager)
    manager._memRecordings = {}

    taken = tmp_path / 'rec_Camera.ome.tiff'
    taken.write_bytes(b'')

    assert manager.getSaveFilePath(str(taken)).endswith('rec_Camera_1.ome.tiff')

    plain = tmp_path / 'rec_Camera.hdf5'
    plain.write_bytes(b'')
    assert manager.getSaveFilePath(str(plain)).endswith('rec_Camera_1.hdf5')

"""``HDF5Storer.writeStack``: a stack already in memory, written as a finished recording.

The live reconstruction tool keeps a run's frames and saves them on request;
the file must be what a recording of the same scan would have been, so
ImProcess and every other reader treat it the same.
"""

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, annotationsFromAttrs
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_TIMELAPSE,
    RecordingPlan,
    build_ome_image_meta,
    build_recording_attrs,
    completed_recording_attrs,
)


class _Detector:
    pixelSizeUm = [1.0, 0.2, 0.1]
    dtype = np.uint16
    isScanDriven = False


class _Manager:
    def __getitem__(self, name):
        return _Detector()


def _attrs(frames):
    planned = build_recording_attrs(
        RecordingPlan('ScanOnce', rec_frames=frames, num_cam_ttl={'CAM': 1}, source_format='memory'),
        'CAM', {'Scan:mode': 'raster', 'notes:session': 'kept from the live tool'},
        expected_frames=frames,
    )
    return completed_recording_attrs(planned, frames)


def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def test_write_stack_produces_the_structured_recording_layout(tmp_path):
    frames = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    storer = HDF5Storer(str(tmp_path / 'stack'), _Manager())
    path = str(tmp_path / 'stack_CAM.h5')

    shape = storer.writeStack(path, 'CAM', frames, _attrs(3))

    assert shape == (3, 4, 5)
    with h5py.File(path, 'r') as file:
        assert _text(file.attrs['rec_mode']) == 'recording'
        dataset = file['CAM/data']
        np.testing.assert_array_equal(dataset[()], frames)
        assert _text(dataset.attrs['detector_name']) == 'CAM'
        assert list(dataset.attrs['element_size_um']) == [1.0, 0.2, 0.1]
        assert not bool(dataset.attrs['writing'])
        assert _text(dataset.attrs['recording:dataset_path']) == '/CAM/data'
        assert _text(dataset.attrs['recording:source_format']) == 'HDF5'
        assert int(dataset.attrs['recording:actual_frames']) == 3
        assert _text(dataset.attrs['recording:completion_outcome']) == 'complete'
        assert 'AcquisitionLayout:json' not in dataset.attrs        # the plan declared none
        assert _text(file['CAM/metadata/Scan'].attrs['mode']) == 'raster'


def test_write_stack_embeds_the_ome_description_when_given(tmp_path):
    frames = np.zeros((3, 4, 5), dtype=np.uint16)
    storer = HDF5Storer(str(tmp_path / 'stack'), _Manager())
    attrs = _attrs(3)
    storer.omeMeta = {'CAM': build_ome_image_meta(
        'CAM', MODE_TIMELAPSE, 3, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16,
        annotations=annotationsFromAttrs(attrs),
    )}
    path = str(tmp_path / 'stack_CAM.h5')

    storer.writeStack(path, 'CAM', frames, attrs)

    with h5py.File(path, 'r') as file:
        assert _text(file['CAM/data'].attrs['axes']).lower() == 'tyx'
        assert 'ome_xml' in file['CAM'].attrs
        assert 'kept from the live tool' in _text(file['CAM'].attrs['ome_xml'])


def test_write_stack_gives_a_single_frame_a_frame_axis_and_never_overwrites(tmp_path):
    storer = HDF5Storer(str(tmp_path / 'stack'), _Manager())
    path = str(tmp_path / 'stack_CAM.h5')

    assert storer.writeStack(path, 'CAM', np.zeros((4, 5), dtype=np.uint16), _attrs(1)) == (1, 4, 5)
    with pytest.raises(FileExistsError):
        storer.writeStack(path, 'CAM', np.zeros((4, 5), dtype=np.uint16), _attrs(1))

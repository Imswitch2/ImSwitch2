"""AcquisitionResult: the structured result of a recording to memory, read
from the recording's HDF5 bytes. The files here have the structure of the
two real ones (dumped from the mock scan setups): a Base camera scan that
records only its plan, and an Advanced point scan that records its layout."""
import io
import json
from datetime import datetime

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.model.acquisition_result import acquisitionResultFromFiles


def _file(detector, data, *, layout=None, scanStage=None, outcome='complete',
          expected=None, detectorParams=None):
    buffer = io.BytesIO()
    with h5py.File(buffer, 'w') as f:
        group = f.create_group(detector)
        dataset = group.create_dataset('data', data=data)
        dataset.attrs['axes'] = 'TYX'
        dataset.attrs['element_size_um'] = np.array([1.0, 0.15, 0.15])
        dataset.attrs['recording:completion_outcome'] = outcome
        dataset.attrs['recording:expected_frames'] = expected or data.shape[0]
        dataset.attrs['recording:actual_frames'] = data.shape[0]
        dataset.attrs['detector_name'] = detector
        if layout is not None:
            dataset.attrs['AcquisitionLayout:json'] = json.dumps(layout)
            dataset.attrs['AcquisitionLayout:schema'] = layout['schema']
        metadata = group.create_group('metadata')
        acquisition = metadata.create_group('acquisition')
        acquisition.attrs['start_time'] = '2026-10-14T18:48:51.293108+00:00'
        det = metadata.create_group('Detector')
        for key, value in (detectorParams or {}).items():
            det.attrs[key] = value
        if scanStage is not None:
            stage = metadata.create_group('ScanStage')
            for key, value in scanStage.items():
                stage.attrs[key] = value
    return buffer.getvalue()


_BASE_SCAN_STAGE = {
    'axis_centerpos': np.array([0., 0., 0.]), 'axis_length': np.array([1., 1., 0.1]),
    'axis_startpos': np.array([[0], [0], [0]]), 'axis_step_size': np.array([0.1, 0.1, 0.1]),
    'positive_direction': np.array([True, True, True]),
    'scan_axis_devices': np.array(['X', 'Y'], dtype='S'),
    'scan_axis_physical': np.array(['X', 'Y'], dtype='S'),
    'sequence_time': 0.01, 'target_device': np.array(['X', 'Y', 'Z'], dtype='S'),
}

_POINT_LAYOUT = {
    'detector': 'APD', 'schema': 'imswitch.acquisition-layout/1',
    'payload_kind': 'assembled-image', 'provenance': 'recorded',
    'storage_axes': ['frame', 'scan_y', 'scan_x'], 'modality': None, 'partitions': [],
    'recorded_event_spans': None, 'scan_source': 'ScanControllerAdvanced',
    'event_loops': [
        {'count': 4, 'device': 'Y', 'direction': 1, 'id': 'scan_y', 'kind': 'scan_y',
         'labels': [], 'step': 0.1, 'storage_axis': 'scan_y', 'unit': 'um'},
        {'count': 5, 'device': 'X', 'direction': 1, 'id': 'scan_x', 'kind': 'scan_x',
         'labels': [], 'step': 0.1, 'storage_axis': 'scan_x', 'unit': 'um'}],
    'traversal': [{'loop_id': 'scan_y', 'order': 'forward', 'parity_loops': []},
                  {'loop_id': 'scan_x', 'order': 'forward', 'parity_loops': []}],
}

_FRAME_LAYOUT = {
    'detector': 'Camera', 'schema': 'imswitch.acquisition-layout/1',
    'payload_kind': 'detector-frame-stream', 'provenance': 'recorded',
    'storage_axes': ['frame', 'detector_y', 'detector_x'], 'modality': None,
    'partitions': [], 'recorded_event_spans': None, 'scan_source': 'ScanControllerAdvanced',
    'event_loops': [
        {'count': 2, 'device': 'Y', 'direction': -1, 'id': 'scan_y', 'kind': 'scan_y',
         'labels': [], 'step': 0.2, 'storage_axis': None, 'unit': 'um'},
        {'count': 3, 'device': 'X', 'direction': 1, 'id': 'scan_x', 'kind': 'scan_x',
         'labels': [], 'step': 0.1, 'storage_axis': None, 'unit': 'um'}],
    'traversal': [{'loop_id': 'scan_y', 'order': 'forward', 'parity_loops': []},
                  {'loop_id': 'scan_x', 'order': 'forward', 'parity_loops': []}],
}


def test_a_base_scan_gets_its_positions_from_its_plan():
    data = np.arange(100 * 2 * 3, dtype=np.uint16).reshape(100, 2, 3)
    content = _file('Camera', data, scanStage=_BASE_SCAN_STAGE,
                    detectorParams={'Camera:Param:Set exposure time': 0.005})
    result = acquisitionResultFromFiles({'rec_Camera.hdf5': content}, ['Camera'])

    np.testing.assert_array_equal(result.array, data)
    camera = result.detector('Camera')
    assert camera.axes == 'TYX' and camera.pixelSizeUm == (1.0, 0.15, 0.15)
    assert camera.complete and result.complete
    assert camera.settings['Detector']['Camera:Param:Set exposure time'] == 0.005
    assert camera.settings['ScanStage']['axis_step_size'] == [0.1, 0.1, 0.1]
    positions = camera.positions
    assert positions.source == 'plan'                      # the Base scan records no layout
    assert positions.devices == ('Z', 'Y', 'X')
    assert positions.indices[[0, 1, 9, 10, 11]].tolist() == [
        [0, 0, 0], [0, 0, 1], [0, 0, 9], [0, 1, 0], [0, 1, 1]]
    assert any('adapted from legacy ScanStage' in note for note in camera.layoutNotes)
    assert result.startedAt == datetime.fromisoformat('2026-10-14T18:48:51.293108+00:00')


def test_a_recorded_frame_layout_gives_recorded_positions_and_offsets():
    data = np.zeros((6, 2, 2))
    content = _file('Camera', data, layout=_FRAME_LAYOUT)
    positions = acquisitionResultFromFiles({'f.hdf5': content}, ['Camera']).detector(
        'Camera').positions
    assert positions.source == 'recorded'
    assert positions.loops == ('scan_y', 'scan_x') and positions.units == ('um', 'um')
    assert positions.indices.tolist() == [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]]
    np.testing.assert_allclose(positions.offsets()[4], [-0.2, 0.1])  # Y ran backwards


def test_an_assembled_point_scan_image_has_no_frame_positions():
    content = _file('APD', np.zeros((1, 4, 5)), layout=_POINT_LAYOUT)
    apd = acquisitionResultFromFiles({'p.hdf5': content}, ['APD']).detector('APD')
    assert apd.payload == 'assembled-image'
    assert apd.positions is None                          # its pixels are the positions


def test_an_incomplete_recording_says_so():
    content = _file('Camera', np.zeros((3, 2, 2)), outcome='stopped_early', expected=10)
    result = acquisitionResultFromFiles({'s.hdf5': content}, ['Camera'])
    camera = result.detector('Camera')
    assert not result.complete and camera.outcome == 'stopped_early'
    assert (camera.framesRecorded, camera.framesExpected) == (3, 10)


def test_several_detectors_show_and_save(tmp_path):
    shown = []
    files = {'a_Camera.hdf5': _file('Camera', np.ones((2, 3, 3))),
             'a_APD.hdf5': _file('APD', np.zeros((1, 4, 5)), layout=_POINT_LAYOUT)}
    result = acquisitionResultFromFiles(files, ['Camera', 'APD'],
                                        show=lambda *args: shown.append(args))
    assert result.detectors == ['Camera', 'APD'] and len(result) == 2
    with pytest.raises(ValueError):
        result.array
    assert set(result.data) == {'Camera', 'APD'}

    result.show('scan')
    assert [(name, scale) for name, _data, scale in shown] == [
        ('scan: Camera', (0.15, 0.15)), ('scan: APD', (0.15, 0.15))]

    paths = result.save(str(tmp_path / 'out'))
    assert sorted(p.split('/')[-1] for p in paths) == ['a_APD.hdf5', 'a_Camera.hdf5']
    with h5py.File(paths[0], 'r') as f:                  # every attribute kept
        assert 'recording:completion_outcome' in f[list(f)[0]]['data'].attrs
    with pytest.raises(FileExistsError):
        result.save(str(tmp_path / 'out'))
    assert len(result.save(str(tmp_path / 'out'), overwrite=True)) == 2
    assert 'Camera: TYX (2, 3, 3)' in repr(result)


def test_a_detector_without_frames_is_named():
    with pytest.raises(RuntimeError, match='no frames for APD'):
        acquisitionResultFromFiles({'c.hdf5': _file('Camera', np.zeros((1, 2, 2)))},
                                   ['Camera', 'APD'])


def test_show_without_an_image_view_says_so():
    result = acquisitionResultFromFiles({'c.hdf5': _file('Camera', np.zeros((1, 2, 2)))},
                                        ['Camera'])
    with pytest.raises(RuntimeError, match='no image view'):
        result.show()

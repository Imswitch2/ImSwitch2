"""The recording plan helpers answer frame accounting and the attribute block
identically for a file, a RAM recording and an in-process live stream.

They are pure functions over a :class:`RecordingPlan`; the recording worker
and the live-reconstruction source both call them, which is what keeps a
reconstructor's view of the geometry the same whichever path fed it.
"""

from dataclasses import replace

import pytest

from imswitch.imcommon.model import (
    ACQUISITION_LAYOUT_SCHEMA,
    decode_acquisition_layout,
    encode_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_layout import PAYLOAD_ASSEMBLED_IMAGE
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_point_scan_layouts,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    SOURCE_FORMAT_MEMORY,
    RecordingPlan,
    build_recording_attrs,
    expected_frames_for,
    planned_frames_from_layout,
)


class _OnePulseEach(dict):
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return 1

    def get(self, key, default=None):
        return 1


def _camera_layout(detector='CAM', x=3, y=2):
    return build_point_scan_layouts(
        {
            'img_dims': [x, y],
            'img_axes_phys': ['x', 'y'],
            'pixel_sizes': [0.1, 0.2],
        },
        (detector,),
        scan_source='ScanControllerPointScan',
        pulse_counts=_OnePulseEach(),
    )[detector]


def test_camera_yields_one_frame_per_scan_position():
    plan = RecordingPlan('ScanOnce', rec_frames=100, num_cam_ttl={'Camera': 1})
    assert expected_frames_for(plan, 'Camera', is_scan_driven=False) == 100

    gated = replace(plan, num_cam_ttl={'Camera': 3})
    assert expected_frames_for(gated, 'Camera', is_scan_driven=False) == 300


@pytest.mark.parametrize('mode', ['ScanOnce', 'ScanLapse'])
def test_scan_driven_detector_records_one_assembled_frame_per_scan(mode):
    plan = RecordingPlan(mode, rec_frames=100, num_cam_ttl={})
    assert plan.is_scan_mode is True
    assert expected_frames_for(plan, 'APD', is_scan_driven=True) == 1


def test_scan_driven_detector_in_frame_mode_records_the_requested_frames():
    plan = RecordingPlan('SpecFrames', rec_frames=100)
    assert plan.is_scan_mode is False
    assert expected_frames_for(plan, 'APD', is_scan_driven=True) == 100


def test_scan_mode_without_declared_pulses_refuses_to_guess():
    plan = RecordingPlan('ScanOnce', rec_frames=42, num_cam_ttl={})
    with pytest.raises(ValueError, match='declares no TTL pulse'):
        expected_frames_for(plan, 'Camera', is_scan_driven=False)


def test_non_scan_mode_defaults_to_one_pulse_per_frame():
    plan = RecordingPlan('UntilStop', rec_frames=42, num_cam_ttl={})
    assert expected_frames_for(plan, 'Camera', is_scan_driven=False) == 42


def test_missing_rec_frames_is_an_error_not_a_type_error():
    plan = RecordingPlan('SpecFrames')
    with pytest.raises(ValueError, match='recFrames must be specified'):
        expected_frames_for(plan, 'Camera', is_scan_driven=False)


def test_recorded_layout_answers_before_the_mode_rules():
    layout = _camera_layout()
    plan = RecordingPlan('ScanOnce', rec_frames=100, num_cam_ttl={'CAM': 7},
                         acquisition_layouts={'CAM': layout})
    assert planned_frames_from_layout(layout) == 6
    assert expected_frames_for(plan, 'CAM', is_scan_driven=False) == 6

    encoded = replace(plan, acquisition_layouts={'CAM': encode_acquisition_layout(layout)})
    assert expected_frames_for(encoded, 'CAM', is_scan_driven=False) == 6

    assembled = replace(layout, payload_kind=PAYLOAD_ASSEMBLED_IMAGE)
    assert planned_frames_from_layout(assembled) == 1
    assert expected_frames_for(
        replace(plan, acquisition_layouts={'CAM': assembled}), 'CAM',
        is_scan_driven=True,
    ) == 1


def test_layout_of_another_detector_does_not_apply():
    plan = RecordingPlan('ScanOnce', rec_frames=10, num_cam_ttl={'Other': 2},
                         acquisition_layouts={'CAM': _camera_layout()})
    assert expected_frames_for(plan, 'Other', is_scan_driven=False) == 20


def test_build_recording_attrs_describes_the_session():
    layout = _camera_layout()
    plan = RecordingPlan(
        'ScanLapse', rec_frames=6, num_cam_ttl={'CAM': 1},
        acquisition_layouts={'CAM': layout}, source_format='ZARR',
        num_timepoints=3, lapse_index=1, single_lapse_file=True,
        lapse_interval_s=2.5, planned_start_time='2026-09-30T10:00:00',
    )
    attrs = build_recording_attrs(
        plan, 'CAM', {'user_key': 'user_value', 'Detector:exposure': '10'},
        expected_frames=6, exposure_time_ms=50.0,
        start_time='2026-09-30T10:00:01+00:00', software_version='9.9.9',
    )

    assert attrs['user_key'] == 'user_value'
    assert attrs['Detector:exposure'] == '10'
    assert attrs['acquisition:software_version'] == '9.9.9'
    assert attrs['acquisition:start_time'] == '2026-09-30T10:00:01+00:00'
    assert attrs['acquisition:exposure_time_ms'] == '50.0'
    assert attrs['recording:detector_name'] == 'CAM'
    assert attrs['recording:source_format'] == 'ZARR'
    assert attrs['recording:expected_frames'] == 6
    assert attrs['recording:planned_frames'] == 6
    assert attrs['recording:frames_per_stack'] == 6
    assert attrs['recording:planned_partitions'] == 1
    assert attrs['AcquisitionLayout:schema'] == ACQUISITION_LAYOUT_SCHEMA
    assert decode_acquisition_layout(attrs['AcquisitionLayout:json']) == layout
    assert attrs['recording:num_timepoints'] == 3
    assert attrs['recording:lapse_index'] == 1
    assert attrs['recording:single_lapse_file'] is True
    assert attrs['recording:lapse_interval_s'] == 2.5
    assert attrs['recording:planned_start_time'] == '2026-09-30T10:00:00'


def test_build_recording_attrs_defaults_and_omissions():
    plan = RecordingPlan('SpecFrames', rec_frames=4)
    attrs = build_recording_attrs(plan, 'CAM', None)

    assert 'recording:expected_frames' not in attrs
    assert 'acquisition:exposure_time_ms' not in attrs
    assert 'AcquisitionLayout:json' not in attrs
    assert 'recording:lapse_interval_s' not in attrs
    assert 'recording:planned_start_time' not in attrs
    assert attrs['recording:num_timepoints'] == 1
    assert attrs['recording:lapse_index'] == 0
    assert attrs['recording:single_lapse_file'] is False
    assert attrs['recording:planned_partitions'] == 1
    assert attrs['recording:source_format'] == 'HDF5'
    assert attrs['acquisition:software_version']
    assert attrs['acquisition:start_time']


def test_build_recording_attrs_keeps_an_existing_partition_count():
    plan = RecordingPlan('SpecFrames', rec_frames=4)
    attrs = build_recording_attrs(plan, 'CAM', {'recording:planned_partitions': 4})
    assert attrs['recording:planned_partitions'] == 4


def test_build_recording_attrs_does_not_mutate_the_input():
    given = {'user_key': 'user_value'}
    build_recording_attrs(RecordingPlan('SpecFrames', rec_frames=1), 'CAM', given)
    assert given == {'user_key': 'user_value'}


def test_layout_must_be_a_layout_or_its_json():
    plan = RecordingPlan('ScanOnce', rec_frames=1, acquisition_layouts={'CAM': 42})
    with pytest.raises(TypeError, match='AcquisitionLayout or encoded JSON'):
        build_recording_attrs(plan, 'CAM', {})
    with pytest.raises(TypeError, match='AcquisitionLayout or encoded JSON'):
        expected_frames_for(plan, 'CAM', is_scan_driven=False)


def test_memory_source_format_is_distinct_from_the_file_formats():
    assert SOURCE_FORMAT_MEMORY == 'memory'
    plan = RecordingPlan('ScanOnce', rec_frames=1, source_format=SOURCE_FORMAT_MEMORY)
    assert build_recording_attrs(plan, 'CAM', {})['recording:source_format'] == 'memory'

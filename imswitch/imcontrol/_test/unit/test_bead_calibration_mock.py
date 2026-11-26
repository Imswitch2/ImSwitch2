"""End to end on the mock: bead recordings as the Recording panel writes them
-> camera->scan calibration -> the smart pipeline's transform.

The real ImControl (``_bead_recording_driver``, in a subprocess) snaps the
camera and records an Advanced scan of the APD on ``simple_mock_setup``
whose virtual sample is the ``beads`` preset, to HDF5 and to Zarr. The mock
camera of that setup images noise, not the sample, so its pixels are
replaced by the same virtual sample rendered through a known camera
geometry (rotated 30 degrees, mirrored, 0.097 um per pixel); everything
else in the files -- layout, metadata, the APD's scan and its geometry --
is what the recorder wrote. The calibration must recover the known
geometry inside the scanned field, and the saved calibration must give the
same numbers through the pipeline's transform step.
"""
import json
import math
import os
import shutil
import site
import subprocess
import sys

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import FrameIdentity, Points, apply_transform
from imswitch.imcontrol.model import bead_calibration as bc
from imswitch.imcontrol.model.smartpipeline.boxes import map_boxes, transform_record
from imswitch.imcontrol.model.smartpipeline.calibrations import identity_problems, load_calibrations

from ._bead_recordings import FIELD, A, T, record_mock_bead_files

@pytest.fixture(scope='module')
def files(tmp_path_factory):
    return record_mock_bead_files(tmp_path_factory.mktemp('bead_rec'))


_A, _T, _FIELD = A, T, FIELD


def _field_error(transform):
    camera_px = (_FIELD - _T) @ np.linalg.inv(_A).T
    return np.abs(transform.map_points(camera_px) - _FIELD)


@pytest.mark.parametrize('camera, scan', [('camera_beads_h5', 'apd_hdf5'), ('camera_beads_zarr', 'apd_zarr')])
def test_the_recorded_files_calibrate_to_the_known_geometry(files, tmp_path, camera, scan):
    result = bc.calibrate_from_files(files[camera], files[scan], folder=tmp_path / 'calibrations')
    # Inside the scanned field, within 80 nm of where the virtual sample is
    # (the mock APD's image sits ~25 nm along the fast axis from the designer's
    # pixel centres: a phase-delay-like offset the fit absorbs).
    assert _field_error(result.transform).max() < 0.08, result.report()
    g = result.registration.geometry()
    assert g['mirror'] and abs(g['rotation_deg'] - 30.0) < 0.3 and abs(g['scale'] / 0.097 - 1) < 0.01
    assert result.registration.target_axes == ('Y', 'X')
    assert result.transform.source_frame == 'detector:Camera'
    assert result.transform.target_frame == 'scanner:galvo'
    assert result.calibration_id == 'cam_to_scan'
    for kind in ('record', 'pipeline', 'figure', 'report'):
        assert os.path.exists(result.paths[kind]), kind
    record = json.loads(open(result.paths['record']).read())
    assert record['source_context']['roi'] == [0, 0, 800, 800]
    assert record['target_context']['ScanStage:phase_delay'] == 0.0
    assert record['target_context']['setup:name'] == 'simple_mock_setup.json'
    assert record['provenance']['beads_used'] >= 6


def test_the_saved_calibration_drives_the_pipeline_transform_step(files, tmp_path):
    folder = tmp_path / 'calibrations'
    result = bc.calibrate_from_files(files['camera_beads_h5'], files['apd_hdf5'], folder=folder)
    registry = load_calibrations(folder)
    assert 'cam_to_scan' in registry
    ref = registry.get('cam_to_scan')
    assert ref.target_axes == ('Y', 'X')
    camera_px = np.array([[10.0, 20.0], [400.0, 380.5], [799.0, 0.0]])
    expected = result.transform.map_points(camera_px)
    np.testing.assert_allclose(ref.apply(camera_px, {}), expected, rtol=0, atol=1e-12)
    moved = apply_transform(Points(camera_px, frame='detector:Camera'), ref)
    assert moved.frame == 'scanner:galvo'
    np.testing.assert_allclose(moved.coordinates, expected, atol=1e-12)
    # Boxes: the four corners through the same transform.
    source = Points(np.array([[100.0, 100.0]]), frame='detector:Camera',
                    properties={'bbox_y0': np.array([90.0]), 'bbox_x0': np.array([95.0]),
                                'bbox_y1': np.array([110.0]), 'bbox_x1': np.array([120.0])})
    box_moved = apply_transform(source, ref)
    map_boxes(source, box_moved, ref, {})
    corners = result.transform.map_points(np.array([[90, 95], [90, 120], [110, 95], [110, 120]], float))
    assert box_moved.properties['bbox_y0'][0] == pytest.approx(corners[:, 0].min())
    assert box_moved.properties['bbox_x1'][0] == pytest.approx(corners[:, 1].max())
    logged = transform_record(ref)
    assert logged['residual_key'] == 'residual_rms_um' and logged['residual'] < 0.05
    # Arming checks: the calibrated grid passes, another ROI or binning is refused.
    assert identity_problems(ref, FrameIdentity(shape=(800, 800))) == []
    assert identity_problems(ref, FrameIdentity(shape=(400, 400), binning=(2, 2)))
    assert identity_problems(ref, FrameIdentity(shape=(800, 800), offset=(16, 0)))


def test_verification_and_one_point_prediction(files, tmp_path):
    folder = tmp_path / 'calibrations'
    bc.calibrate_from_files(files['camera_beads_h5'], files['apd_hdf5'], folder=folder)
    report = bc.verify_from_files('cam_to_scan', files['camera_beads_zarr'], files['apd_zarr'], folder=folder)
    assert report['matched'] >= 6 and report['rms_um'] < 0.05 and report['grid_problems'] == []
    transform = bc.load_bead_calibration('cam_to_scan', folder)
    camera_px = (np.array([[-2.0, 3.0]]) - _T) @ np.linalg.inv(_A).T
    centre = bc.predict_scan_centre(transform, *camera_px[0])
    assert centre['Y'] == pytest.approx(-2.0, abs=0.08) and centre['X'] == pytest.approx(3.0, abs=0.08)


def test_command_line(files, tmp_path, capsys):
    folder = str(tmp_path / 'calibrations')
    assert bc.main([files['camera_beads_h5'], files['apd_hdf5'], '--folder', folder]) == 0
    assert 'run-scan with axes ["Y", "X"]' in capsys.readouterr().out
    assert bc.main(['predict', 'cam_to_scan', '400', '400', '--folder', folder]) == 0
    assert set(json.loads(capsys.readouterr().out.strip().splitlines()[-1])) == {'Y', 'X'}
    # The raw mock camera images noise: refused, not a wrong transform.
    assert bc.main([files['camera_h5'], files['apd_hdf5'], '--folder', folder, '--id', 'noise']) == 3


def test_reading_files_the_user_will_have(files, tmp_path):
    import h5py

    two = tmp_path / 'two.h5'
    shutil.copy(files['camera_beads_h5'], two)
    with h5py.File(two, 'r+') as handle:
        handle.copy('Camera', 'Other')
        data = handle['Camera/data'][()]
        del handle['Camera/data']
        handle['Camera/data'] = np.concatenate([data, data + 2], axis=0)      # a 2-frame stack
    with pytest.raises(bc.BeadCalibrationError, match='name the one to use'):
        bc.read_recording(two)
    recording = bc.read_recording(two, 'Camera')
    assert recording.frames == 2 and recording.dtype == 'uint16'
    np.testing.assert_allclose(recording.image, data[0].astype(float) + 1.0)
    missing = tmp_path / 'missing.hdf5'
    shutil.copy(files['apd_hdf5'], missing)
    with h5py.File(missing, 'r+') as handle:
        stage = handle['APD/metadata/ScanStage']
        for key in list(stage.attrs):
            if 'axis_step_size' in key:
                del stage.attrs[key]
        for key in list(stage.keys()):
            if 'axis_step_size' in key:
                del stage[key]
    with pytest.raises(bc.BeadCalibrationError, match='axis_step_size'):
        bc.scan_geometry_from_recording(bc.read_recording(missing))
    geometry = bc.scan_geometry_from_recording(bc.read_recording(files['apd_hdf5']))
    fast, slow = geometry['axes']
    assert (fast['device'], fast['count'], slow['device'], slow['count']) == ('X', 240, 'Y', 200)
    assert fast['first_um'] == pytest.approx(3.0 - 12.0 + 0.05) and slow['first_um'] == pytest.approx(-2.0 - 10 + 0.05)

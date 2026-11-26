"""The smart pipeline's triggered scan is saved, end to end on the mock.

The real ImControl (``_triggered_scan_save_driver``, in a subprocess) runs
the ``run-scan`` action's real adapter against the real Advanced scan
controller on the simulated NI-DAQ, with the run's recorder: every scan is
started by a Scan-once recording of the APD into the run's folder, one file
per event, with the event's metadata. A recording the Recording controller
refuses fails the action without scanning; the Smart Pipeline panel refuses
to arm in Experiment when the recordings folder cannot be written.
"""
import json
import os
import site
import subprocess
import sys

import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.SharedAttributes import SharedAttributes
from imswitch.imcontrol.model.smartpipeline.event_recording import (
    event_attrs, event_file_name, folder_problems, recorded_files,
)

_DRIVER = 'imswitch.imcontrol._test.unit._triggered_scan_save_driver'
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 4)))


@pytest.fixture(scope='module')
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp('triggered_scan')
    env = dict(os.environ, HOME=str(out / 'home'), QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg',
               PYTHONUSERBASE=site.getuserbase(),
               PYTHONPATH=_REPO + os.pathsep + os.environ.get('PYTHONPATH', ''))
    os.makedirs(env['HOME'], exist_ok=True)
    proc = subprocess.run([sys.executable, '-m', _DRIVER, str(out)], env=env, cwd=_REPO,
                          capture_output=True, text=True, timeout=170)
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(line for line in proc.stdout.splitlines() if line.startswith('RESULT '))
    return json.loads(line[len('RESULT '):])


def _attrs(path):
    with h5py.File(path, 'r') as handle:
        return SharedAttributes.fromHDF5File(handle, 'APD').getHDF5Attributes(), handle['APD/data'][()]


def test_each_triggered_scan_is_saved_as_a_scan_once_recording_with_the_event(report):
    adapter = report['adapter']
    assert adapter['detectors'] == ['APD'] and adapter['problems'] == []
    paths = []
    for index, (event, label) in enumerate(zip(adapter['events'], (3, 7)), start=1):
        assert event['finished'] and event['outcome'] == 'ok', event['error']
        saved = event['result']['saved']
        assert len(saved) == 1 and os.path.basename(saved[0]).startswith(event_file_name(index, label, 0))
        attrs, data = _attrs(saved[0])
        # The file holds the scan the detector published for that trigger.
        assert data.shape[0] == 1
        np.testing.assert_array_equal(data[0], np.asarray(event['latest'], dtype=data.dtype))
        # The event's metadata, next to the scan's own (geometry written by the recorder).
        assert attrs['SmartPipeline:event:roi_label'] == label
        assert attrs['SmartPipeline:event:roi_id'] == f'R0-{label}'
        assert attrs['SmartPipeline:event:event'] == index
        assert attrs['SmartPipeline:event:dff'] == pytest.approx(0.8)
        assert attrs['SmartPipeline:event:F0'] == pytest.approx(100.0)
        assert attrs['SmartPipeline:event:transform_id'] == 'cam_to_scan'
        assert attrs['SmartPipeline:event:transform_residual'] == pytest.approx(0.031)
        assert json.loads(attrs['SmartPipeline:event:bbox_camera_px']) == [10.5, 20.5, 18.5, 28.5]
        assert json.loads(attrs['SmartPipeline:event:stage_position_um']) == {'Stage': {'X': 12.5, 'Y': -3.0}}
        assert attrs['SmartPipeline:pipeline'] == 'driver'
        times = [attrs[f'SmartPipeline:event:{k}'] for k in
                 ('frame_wall', 'decision_wall', 'scan_start_wall', 'scan_end_wall')]
        assert times == sorted(times), times
        assert 'start_request' in json.loads(attrs['SmartPipeline:event:latency_s'])
        assert any(key.startswith('ScanStage') or key.startswith('Scan') for key in attrs), sorted(attrs)[:20]
        paths.append(saved[0])
    assert len(set(paths)) == 2


def test_the_saved_scan_opens_in_improcess(report):
    from imswitch.improcess.model import DataObj

    path = report['adapter']['events'][0]['result']['saved'][0]
    names = DataObj.getDatasetNames(path)
    assert 'APD' in names
    data = DataObj(os.path.basename(path), 'APD', path=path)
    data.checkAndLoadData()
    assert np.asarray(data.data).shape[-2:] == _attrs(path)[1].shape[-2:]
    data.checkAndUnloadData()


def test_saving_adds_no_latency_before_the_scan_starts(report):
    saved = report['adapter']['events'][1]['result']['latency_s']['start_request']
    bare = report['adapter']['bare']['latency_s']['start_request']
    # The recording arms its writer as part of the scan start; on the mock
    # that is within the scatter of a bare start (both tens of ms).
    assert saved < bare + 0.1, (saved, bare)


def test_a_scan_that_cannot_be_saved_is_not_started_and_fails_the_action(report):
    refused = report['adapter']['refused']
    assert refused['outcome'] == 'failed'
    assert 'could not be saved' in refused['error'] and 'disk full (simulated)' in refused['error']
    assert refused['scans_started'] == 0


def test_experiment_refuses_to_arm_without_a_writable_folder_and_validate_says_so(report):
    arm = report['arm']
    assert arm['load_issues'] == []
    assert not arm['experiment']['armed']
    assert 'could not be saved' in arm['experiment']['issues'] and 'is a file' in arm['experiment']['issues']
    assert arm['validate']['armed']
    assert 'in Experiment the triggered scans could not be saved' in arm['validate']['issues']


# ------------------------------------------------------------ pure helpers

def test_folder_problems(tmp_path):
    assert folder_problems(str(tmp_path / 'a' / 'b')) == []
    assert not (tmp_path / 'a').exists()          # a check creates nothing
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    assert 'is a file' in folder_problems(str(blocker / 'sub'))[0]
    assert folder_problems('') and 'no recordings folder' in folder_problems('')[0]
    assert 'MB free' in folder_problems(str(tmp_path), min_free_bytes=1 << 62)[0]


def test_event_names_and_attrs(tmp_path):
    assert event_file_name(3, 12, 0) == 'event0003_R0_roi12'
    assert event_file_name(3, 12.0, None) == 'event0003_roi12'
    attrs = event_attrs({'roi_label': 4, 'box': [1, 2], 'none': None, 'flag': True})
    assert attrs == {'SmartPipeline:event:roi_label': 4, 'SmartPipeline:event:box': '[1, 2]',
                     'SmartPipeline:event:flag': True}
    (tmp_path / 'e_rec_APD.hdf5').write_text('x')
    (tmp_path / 'e_rec_APD.hdf5.tmp').write_text('x')
    assert recorded_files(str(tmp_path / 'e')) == [str(tmp_path / 'e_rec_APD.hdf5')]

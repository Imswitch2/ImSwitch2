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


def test_the_event_carries_the_keys_shared_with_the_etsted_route(report):
    path = report['adapter']['events'][0]['result']['saved'][0]
    attrs, _ = _attrs(path)
    leaf = {key.rsplit(':', 1)[1]: value for key, value in attrs.items() if key.startswith('SmartPipeline:event:')}
    assert leaf['route'] == 'smart-pipeline' and leaf['event_index'] == 1 and leaf['session_id'] == 'run1'
    assert leaf['pipeline_name'] == 'driver' and leaf['transform_name'] == 'cam_to_scan'
    assert leaf['scan_axes'] == 'Y,X' and np.asarray(leaf['scan_center_um']).shape == (2,)
    assert leaf['event_coords_order'] == 'column,row'
    np.testing.assert_allclose(leaf['event_coords_px'], [24.5, 14.5])       # (column, row) of the box centre
    from datetime import datetime
    times = [datetime.fromisoformat(leaf[k]) for k in
             ('trigger_frame_time_utc', 'scan_start_time_utc', 'scan_end_time_utc')]
    assert all(t.utcoffset().total_seconds() == 0 for t in times) and times == sorted(times)


def test_an_experiment_run_saves_the_context_of_each_event_and_a_summary(report):
    experiment = report['experiment']
    assert experiment['armed'] and experiment['error'] is None, experiment
    runs = sorted({f.split(os.sep)[1] for f in experiment['files'] if f.count(os.sep) >= 2})
    assert len(runs) == 1
    folder = os.path.join(experiment['folder'], 'smart_pipeline', runs[0])
    names = set(os.listdir(folder))
    assert {'pipeline.json', 'run.json', 'summary.csv', 'summary.json', 'region_R0.h5'} <= names
    scans = sorted(n for n in names if n.endswith('_rec_APD.hdf5'))
    contexts = sorted(n for n in names if n.endswith('_context.h5'))
    assert len(scans) >= 2 and len(contexts) == len(scans)
    with open(os.path.join(folder, 'pipeline.json'), encoding='utf-8') as handle:
        assert json.load(handle)['name'] == 'ROI dF/F triggered APD scan'
    with open(os.path.join(folder, 'run.json'), encoding='utf-8') as handle:
        run = json.load(handle)
    assert run['detector'] == 'Camera' and run['scan_detectors'] == ['APD'] and 'tf' in run['transforms']
    with h5py.File(os.path.join(folder, contexts[0]), 'r') as handle:
        roles = [r.decode() for r in handle['camera/role'][()]]
        assert roles.count('trigger') == 1 and roles[0] == 'pre' and roles[-1] == 'post'
        frames = handle['camera/frames']
        assert frames.shape[0] == len(roles) and frames.shape[1:] == handle['rois/labels'].shape
        event = json.loads(handle.attrs['SmartPipeline:event'])
        label = event['roi_label']
        table = handle['rois/table']
        assert label in table['label'][()].tolist()
        traces = handle['traces']
        column = traces['label'][()].tolist().index(label)
        assert traces['dff'].shape[1] == table['label'].shape[0]
        # the trace reaches the triggering frame: its dF/F is the event's
        assert np.any(np.isclose(traces['dff'][:, column], event['dff'], rtol=1e-4))
        trigger_wall = handle['camera/wall_time'][roles.index('trigger')]
        assert trigger_wall == pytest.approx(event['frame_wall'])
        walls = handle['camera/wall_time'][()]
        assert all(t > event['scan_end_wall'] for t, r in zip(walls, roles) if r == 'post')
    with open(os.path.join(folder, 'summary.json'), encoding='utf-8') as handle:
        summary = json.load(handle)
    scanned = [row for row in summary['rows'] if row['status'] == 'scanned']
    assert len(scanned) == len(scans)
    assert {row['scan_file'] for row in scanned} == set(scans)
    assert all(row['roi_id'] == f"R0-{row['roi_label']}" and row['centroid_y_px'] is not None for row in scanned)
    with h5py.File(os.path.join(folder, 'region_R0.h5'), 'r') as handle:
        table = handle['rois/table']
        assert int(table['scanned'][()].sum()) == len(scans)
        assert handle['rois/labels'].shape == (800, 800)


def test_the_archive_writes_rows_and_reports_a_failed_write(tmp_path):
    from imswitch.imcontrol.model.smartpipeline.event_archive import EventArchive

    reports = []
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    archive = EventArchive(str(blocker / 'run'), report=reports.append)
    archive.start({'pipeline': 'p'})
    archive.add_row({'status': 'skipped', 'roi_label': 3, 'reason': 'outside the scan range'})
    assert archive.close(10)
    assert reports and archive.failures                  # reported, nothing raised
    archive = EventArchive(str(tmp_path / 'run'), report=reports.append)
    archive.start({'pipeline': 'p'}, '{"name": "p"}')
    archive.add_row({'status': 'skipped', 'roi_label': 3, 'reason': 'outside the scan range'})
    assert archive.close(10)
    rows = json.loads((tmp_path / 'run' / 'summary.json').read_text())['rows']
    assert rows[0]['roi_id'] == 'R0-3' and rows[0]['status'] == 'skipped'
    assert (tmp_path / 'run' / 'pipeline.json').exists() and (tmp_path / 'run' / 'region_R0.h5').exists()


def test_a_trigger_while_the_panel_is_scanning_is_skipped_with_the_reason(report):
    busy = report['adapter']['busy']
    assert busy['running_during'], busy          # the long scan was still running when the trigger gave up
    assert busy['outcome'] == 'ok' and busy['result']['scanned'] is False, busy
    assert "is busy: a scan is running" in busy['result']['skipped'][0]['reason']
    assert busy['saved'] == 0
    assert busy['long_outcome'] == 'ok', busy['long_error']


def test_scan_panel_busy_reads_the_scan_controllers_state():
    from types import SimpleNamespace

    from imswitch.imcontrol.model.smartpipeline.live_actions import scan_panel_busy

    assert scan_panel_busy(SimpleNamespace(isRunning=False)) == ''
    assert scan_panel_busy(SimpleNamespace(isRunning=True)) == 'a scan is running'
    assert scan_panel_busy(SimpleNamespace(isRunning=False, _stopInFlight=True)) == 'its scan is stopping'
    assert scan_panel_busy(SimpleNamespace(isRunning=False, _scanRunToken=object())) == 'a scan run is still open'
    assert scan_panel_busy(SimpleNamespace(isRunning=False, _repeatPending=True)) == 'a repeated scan is pending'
    assert scan_panel_busy(SimpleNamespace(isRunning=False, _isBusy=lambda: True)) == 'a scan is running'


def test_arming_checks_the_rig_and_says_so_in_one_list(report):
    preflight = report['preflight']
    assert preflight['rois'] == [1, 2]
    validate = preflight['validate']
    assert validate['armed']
    import html
    import re

    text = html.unescape(re.sub(r'<[^>]+>', '\n', validate['preflight']))
    for expected in ("Camera: Camera: 800x800 px, binning 1", "Calibration: 'cam_to_scan'", "'Scan' panel (Advanced)",
                     "Laser: '561 (EXC)'", '2 ROIs: 1 scan whole, 0 clipped to the scan range, 1 would be skipped',
                     "Laser: '561 (EXC)' at 0 V: its power is 0, the scan will be dark",
                     'ROI 2: would be SKIPPED at its trigger', 'Scan time: about', 'Interval: rest: at most one scan',
                     'Budget: budget: at most 10 scans', 'Saving: (Experiment would save to)', 'Scan detectors: APD'):
        assert expected in text, (expected, text)
    assert validate['log']                         # and the run's log keeps them
    wrong = preflight['wrong_laser']
    assert not wrong['armed']
    assert "'560 nm' is not a laser of this setup" in wrong['issues'] and '561 (EXC)' in wrong['issues']

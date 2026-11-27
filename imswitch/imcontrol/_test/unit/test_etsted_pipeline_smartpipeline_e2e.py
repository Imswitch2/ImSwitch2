"""The shipped etSTED dF/F smart pipeline, end to end on the etSTED mock setup, in Validate mode.

The real ImControl (``_etsted_smartpipeline_driver``, in a subprocess) on ``etsted_advanced_mock_setup`` (mock
widefield camera, APD, simulated NI-DAQ, Advanced scan panel): ``imcontrol_etsted/analysis_pipelines/dff_event.py``
and ``smart_pipelines/etsted_dff_event.json`` come with the shipped user defaults; the document is loaded in the
Smart Pipeline panel and armed in Validate on a camera whose frames show a bright spot once it is switched on.
The dry run must say it WOULD scan at the spot (through the camera -> scan calibration), start nothing, and a
control edited while armed must reach the pipeline step.
"""
import json
import os
import site
import subprocess
import sys

import pytest

_DRIVER = 'imswitch.imcontrol._test.unit._etsted_smartpipeline_driver'
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 4)))

pytestmark = [pytest.mark.ui, pytest.mark.timeout(400)]


@pytest.fixture(scope='module')
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp('etsted_smartpipeline')
    env = dict(os.environ, HOME=str(out / 'home'), QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg',
               PYTHONUSERBASE=site.getuserbase(),
               PYTHONPATH=_REPO + os.pathsep + os.environ.get('PYTHONPATH', ''))
    os.makedirs(env['HOME'], exist_ok=True)
    proc = subprocess.run([sys.executable, '-m', _DRIVER, str(out)], env=env, cwd=_REPO,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next((l for l in proc.stdout.splitlines() if l.startswith('RESULT ')), None)
    assert line is not None, (proc.stderr or proc.stdout)[-4000:]
    return json.loads(line[len('RESULT '):])['validate']


def test_the_shipped_defaults_load_in_the_panel_and_expose_the_pipelines_parameters(report):
    assert report['defaults_synced'] == {'pipeline': True, 'document': True}
    assert report['load_issues'] == []
    assert report['controls'] == ['baseline_frames', 'bin_px', 'cooldown_s', 'dff_threshold', 'max_scans',
                                  'min_baseline', 'smooth_px', 'warmup_frames']
    assert 'Raise it until a quiet sample never triggers' in report['control_help']


def test_validate_says_where_it_would_scan_and_scans_nothing(report):
    assert report['armed'], report['arm_issues']
    assert report['ticks_before_event'] >= 14 and report['would_before_event'] == 0      # quiet sample: no event
    assert report['set_threshold'] is True and report['threshold_in_step'] == 0.4          # a control, while armed
    assert len(report['would']) >= 1
    would = report['would'][0]
    # the spot at row 200, column 300 of the camera, with 0.05 um pixels about the frame's centre (256, 256):
    # the pipeline's bin centre (row 201.5, column 301.5) -> y = -2.725 um, x = 2.275 um
    assert would['message'] == 'would run-scan to (y=-2.725, x=2.275) in scanner:galvo'
    first = would['inputs'][0]
    assert first['frame'] == 'scanner:galvo' and first['transform']['id'] == 'cam_to_scan'
    assert first['first_row']['etsted_c1'] == 302.0 and first['first_row']['etsted_c2'] == 202.0   # (column, row)
    assert report['started_events'] == 0 and report['summary_state']['error'] is None
    assert report['run_state_after_stop'] == 'stopped'

"""Structured results against the real ImControl on mock setups: a point
scan's recorded layout, and a frames recording without a scan."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.nohardware

POINT_SCAN = textwrap.dedent('''
    import os, imswitch
    params = os.path.join(os.path.dirname(imswitch.__file__), '_data', 'user_defaults',
                          'scripts', 'tutorial', 'scanning', 'scan_params', 'point_scan_5um.json')
    with temporaryScanParameters():
        api.imcontrol.loadScanParamsFromFile(params)
        result = api.imcontrol.recordScanToMemory('APD', timeout=120, structured=True)
    apd = result.detector('APD')
    assert result.complete, apd.outcome
    assert apd.data.shape == (1, 50, 50), apd.data.shape
    assert apd.payload == 'assembled-image' and apd.positions is None
    assert apd.pixelSizeUm[-2:] == (0.1, 0.1), apd.pixelSizeUm
    assert apd.settings['ScanStage']['axis_length'][:2] == [5.0, 5.0]
    print('POINT-OK', result)
''')

FRAMES = textwrap.dedent('''
    result = api.imcontrol.recordFramesToMemory('Mock Camera', 3, timeout=60, structured=True)
    camera = result.detector('Mock Camera')
    assert result.complete and camera.data.shape[0] == 3
    assert camera.settings['Rec']['Mode'] == 'SpecFrames', camera.settings.get('Rec')
    assert camera.positions is None or camera.positions.source == 'inferred'
    print('FRAMES-OK', result, camera.positions and camera.positions.loops)
''')


@pytest.mark.skipif(os.name == 'nt', reason='the runner cannot isolate the user folder on Windows')
@pytest.mark.parametrize('setup, script, marker', [
    ('galvo_apd_mock_scan_setup.json', POINT_SCAN, 'POINT-OK'),
    ('example_no_hardware.json', FRAMES, 'FRAMES-OK'),
])
def test_structured_results_in_the_app(setup, script, marker, tmp_path):
    path = tmp_path / 'structured.py'
    path.write_text(script, encoding='utf-8')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg')
    completed = subprocess.run(
        [sys.executable, '-m', 'imswitch.imscripting._test.tutorial_runner',
         setup, str(path), '--timeout', '120', '--home', str(tmp_path / 'home')],
        capture_output=True, text=True, env=env, timeout=200,
    )
    lines = completed.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines and lines[-1].startswith('{') else {}
    assert summary.get('status') == 'succeeded', (
        f'{summary}\n--- stdout ---\n{completed.stdout[-3000:]}\n'
        f'--- stderr ---\n{completed.stderr[-3000:]}'
    )
    assert marker in completed.stdout

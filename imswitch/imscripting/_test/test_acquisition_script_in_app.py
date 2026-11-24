"""Copy acquisition as Python against the real ImControl on mock setups: a
script made from the settings, run after the settings were changed, puts
them back and acquires as they were."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.nohardware

SCAN_SETUP = textwrap.dedent('''
    import json, os
    from imswitch.imscripting.model.acquisition_script import acquisitionScript
    import imswitch
    params = os.path.join(os.path.dirname(imswitch.__file__), '_data', 'user_defaults',
                          'scripts', 'tutorial', 'scanning', 'scan_params', 'camera_scan_1um.json')
    api.imcontrol.loadScanParamsFromFile(params)
    api.imcontrol.setDetectorParameter('Camera', 'Trigger source', 'External "frame-trigger"')
    api.imcontrol.setDetectorParameter('Camera', 'Set exposure time', 0.005)
    api.imcontrol.setRecModeScanOnce()
    api.imcontrol.setDetectorToRecord('Camera')
    text = acquisitionScript(api.imcontrol)

    # Change what the script must put back.
    changed = api.imcontrol.getScanParameters()
    changed['analogParameterDict']['axis_length'][0] = 0.5
    api.imcontrol.setScanParameters(changed)
    api.imcontrol.setDetectorParameter('Camera', 'Set exposure time', 0.02)

    namespace = {'api': api, 'show': show}
    exec(compile(text, '<copied acquisition>', 'exec'), namespace)
    assert namespace['frames'].shape[0] == 100, namespace['frames'].shape
    assert api.imcontrol.getDetectorParameter('Camera', 'Set exposure time') == 0.005
    print('COPIED-SCAN-OK')
''')

FRAMES_SETUP = textwrap.dedent('''
    from imswitch.imscripting.model.acquisition_script import acquisitionScript
    api.imcontrol.setRecModeSpecFrames(4)
    api.imcontrol.setDetectorToRecord('Mock Camera')
    api.imcontrol.setRotatorPosition('Mock HWP', 30.0)
    text = acquisitionScript(api.imcontrol)
    assert "'Mock HWP': 30.0" in text
    api.imcontrol.setRecModeSpecFrames(9)
    api.imcontrol.setRotatorPosition('Mock HWP', 10.0)

    namespace = {'api': api, 'show': show}
    exec(compile(text.replace('MOVE_TO_POSITIONS = False', 'MOVE_TO_POSITIONS = True'),
                 '<copied acquisition>', 'exec'), namespace)
    assert namespace['frames'].shape[0] == 4, namespace['frames'].shape
    assert abs(api.imcontrol.getRotatorPosition('Mock HWP') - 30.0) < 0.5
    print('COPIED-FRAMES-OK')
''')


@pytest.mark.skipif(os.name == 'nt', reason='the runner cannot isolate the user folder on Windows')
@pytest.mark.parametrize('setup, script, marker', [
    ('hamamatsu_mock_scan_setup.json', SCAN_SETUP, 'COPIED-SCAN-OK'),
    ('example_no_hardware.json', FRAMES_SETUP, 'COPIED-FRAMES-OK'),
])
def test_a_copied_acquisition_reproduces_the_settings(setup, script, marker, tmp_path):
    path = tmp_path / 'copy_and_run.py'
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

"""The adaptive-optics scripts (``scripts/workflows/adaptive_optics``) run
headless on the AO mock setup, as the tutorials are: the sensor finds its
lenslets, the mirror is calibrated, and the closed loop removes the
simulated sensor's aberration. A smaller mirror (25 actuators, 10 lenslets
across) keeps it quick.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import imswitch

pytestmark = pytest.mark.nohardware

USER_DEFAULTS = Path(imswitch.__file__).parent / '_data' / 'user_defaults'
SCRIPTS = USER_DEFAULTS / 'scripts' / 'workflows' / 'adaptive_optics'
SETUP = USER_DEFAULTS / 'imcontrol_setups' / 'adaptive_optics_mock_setup.json'


def _run(setup, script, home, tmp_path, replace=None):
    text = script.read_text(encoding='utf-8')
    for name, value in (replace or {}).items():
        text, count = re.subn(rf'^{name} = .*$', f'{name} = {value}', text, flags=re.M)
        assert count == 1, name
    path = tmp_path / script.name
    path.write_text(text, encoding='utf-8')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg')
    completed = subprocess.run(
        [sys.executable, '-m', 'imswitch.imscripting._test.tutorial_runner',
         str(setup), str(path), '--timeout', '120', '--home', str(home)],
        capture_output=True, text=True, env=env, timeout=170,
    )
    lines = completed.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines and lines[-1].startswith('{') else {}
    assert completed.returncode == 0 and summary.get('status') == 'succeeded', (
        f'{script.name}: {summary or "no summary"}\n--- stdout ---\n{completed.stdout[-3000:]}\n'
        f'--- stderr ---\n{completed.stderr[-3000:]}')
    return completed.stdout


@pytest.mark.skipif(os.name == 'nt', reason='the runner cannot isolate '
                    'Documents\\ImSwitchConfig from the real one on Windows')
def test_sensor_calibration_and_closed_loop_remove_the_aberration(tmp_path):
    setup = json.loads(SETUP.read_text(encoding='utf-8'))
    setup['deformableMirrors']['DM']['managerProperties']['actuators'] = 25
    setup['detectors']['WFS']['managerProperties']['mockShackHartmann']['lensletsAcross'] = 10
    setupPath = tmp_path / 'ao_setup.json'
    setupPath.write_text(json.dumps(setup), encoding='utf-8')
    home = tmp_path / 'home'
    home.mkdir()

    sensor = _run(setupPath, SCRIPTS / '01_wavefront_sensor.py', home, tmp_path,
                  {'REFERENCE': "'lattice'"})            # the mock has no flat beam
    astigmatism = float(re.search(r'Z5 astigmatism \(oblique\)\s+([-+.\d]+)', sensor).group(1))
    assert astigmatism == pytest.approx(0.08, abs=0.02)   # the mock's aberration

    _run(setupPath, SCRIPTS / '02_mirror_calibration.py', home, tmp_path)
    folder = home / 'ImSwitchConfig' / 'adaptive_optics' / 'DM'
    assert len(list(folder.glob('mirror_calibration_*.npz'))) == 1
    assert len(list(folder.glob('zernike_modes_*.csv'))) == 1

    loop = _run(setupPath, SCRIPTS / '03_closed_loop.py', home, tmp_path)
    errors = [float(e) for e in re.findall(r'Step +\d+: ([.\d]+) ', loop)]
    assert errors[-1] < errors[0] / 5, errors

"""The polarization rig's stack script (``scripts/workflows/polarization``)
run headless on the rig's mock setup, as the tutorials are
(:mod:`.test_shipped_tutorials`): one recorded stack per pair of waveplate
angles, each holding the angles it was taken at. The mock Hamamatsu is on its
external trigger, as on the rig: the simulated Nucleo's camera pulses reach it
(``scan_engines/simulated_frames.py``).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import imswitch

pytestmark = pytest.mark.nohardware

USER_DEFAULTS = Path(imswitch.__file__).parent / '_data' / 'user_defaults'
SCRIPT = USER_DEFAULTS / 'scripts' / 'workflows' / 'polarization' / '01_polarization_stacks.py'
SETUP = USER_DEFAULTS / 'imcontrol_setups' / 'nucleo_polarization_mock_setup.json'


def _twoAnglePairs(tmp_path):
    text = SCRIPT.read_text(encoding='utf-8')
    line = next(line for line in text.splitlines() if line.startswith('ANGLE_PAIRS_DEG = '))
    path = tmp_path / SCRIPT.name
    path.write_text(text.replace(line, 'ANGLE_PAIRS_DEG = [(0.0, 0.0), (22.5, 45.0)]'),
                    encoding='utf-8')
    return path


@pytest.mark.skipif(os.name == 'nt', reason='the runner cannot isolate '
                    'Documents\\ImSwitchConfig from the real one on Windows')
def test_one_stack_per_angle_pair_holding_its_angles(tmp_path):
    import h5py

    from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm

    setup = json.loads(SETUP.read_text(encoding='utf-8'))
    assert setup['detectors']['Hamamatsu']['managerProperties']['hamamatsu'][
        'trigger_source'] == 2                            # external, as on the rig
    home = tmp_path / 'home'
    scans = home / 'ImSwitchConfig' / 'imcontrol_scans' / 'polarization_rig'
    scans.mkdir(parents=True)
    for name, state in lm.rig_scans(lm.Wiring.from_setup(setup), pairs=1).items():
        (scans / f'{name}.json').write_text(json.dumps(state), encoding='utf-8')

    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg')
    completed = subprocess.run(
        [sys.executable, '-m', 'imswitch.imscripting._test.tutorial_runner',
         str(SETUP), str(_twoAnglePairs(tmp_path)), '--timeout', '120',
         '--home', str(home)],
        capture_output=True, text=True, env=env, timeout=170,
    )
    lines = completed.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines and lines[-1].startswith('{') else {}
    assert completed.returncode == 0 and summary.get('status') == 'succeeded', (
        f'{summary or "no summary"}\n--- stdout ---\n{completed.stdout[-3000:]}\n'
        f'--- stderr ---\n{completed.stderr[-3000:]}')

    stacks = sorted((home / 'ImSwitchConfig' / 'recordings').rglob('polarization_*.hdf5'))
    assert [p.name for p in stacks] == [
        'polarization_wide_field_2d_A000p0_B000p0_rec_Hamamatsu.hdf5',
        'polarization_wide_field_2d_A022p5_B045p0_rec_Hamamatsu.hdf5']
    for path, angles in zip(stacks, [(0.0, 0.0), (22.5, 45.0)]):
        with h5py.File(path, 'r') as f:
            assert f['Hamamatsu/data'].shape[0] == 3 + 2      # dark frames, one pair
            rotator = f['Hamamatsu/metadata/Rotator'].attrs
            assert (float(rotator['WaveplateA:Position']),
                    float(rotator['WaveplateB:Position'])) == angles

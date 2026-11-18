"""Mirror calibration -- each actuator's effect on the sensor, and the mirror's Zernike modes.

Pushes every actuator up and down by AMPLITUDE over the flat, as the old
GUI's influence-function run did, and reads the sensor each time
(01_wavefront_sensor.py must have saved it). From that it saves, in
<ImSwitchConfig>/adaptive_optics/<MIRROR>/:

  * mirror_calibration_<time>.npz -- the influence matrix (slopes per unit
    push) the closed loop inverts;
  * zernike_modes_<time>.csv -- the command for 1 µm RMS of each Zernike
    mode in MODES (Noll), a mirror mode matrix: with it as the mirror's
    ``modesFile`` (and ``firstMode`` the first of MODES) the Deformable
    Mirror panel and ``setMirrorModes`` set modes in µm RMS.

The mirror is flat again afterwards, also after Stop.

Mock setup: adaptive_optics_mock_setup.json.
"""
# ruff: noqa: F821
import glob
import os
import time

import numpy as np

from imswitch.imcommon.algorithms import adaptive_optics as ao
from imswitch.imcommon.algorithms import shack_hartmann as sh
from imswitch.imcommon.algorithms import zernike as z
from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.model.managers.deformableMirrors import mirror_files

WFS_CAMERA = 'WFS'
MIRROR = 'DM'

AMPLITUDE = 0.1                 # push per actuator, mirror units (the old GUI's 0.1)
SETTLE_S = 0.05                 # after each command
FRAMES = 1                      # averaged per reading
MODES = z.modes_up_to(21)       # Noll 2-21 in the mode matrix
# Singular values kept when inverting: None keeps those above 1 % of the
# largest. The printout lists them; fewer gives smoother, weaker control.
N_SINGULAR = None

FOLDER = os.path.join(dirtools.UserFileDirs.Root, 'adaptive_optics', MIRROR)
SENSOR_FILE = os.path.join(FOLDER, 'wavefront_sensor.npz')


def grab(frames=FRAMES):
    stack = np.asarray(api.imcontrol.recordFramesToMemory(WFS_CAMERA, frames + 1), dtype=float)
    return stack[1:].mean(axis=0)


def readAt(command):
    api.imcontrol.setMirrorCommand(MIRROR, [float(v) for v in command])
    time.sleep(SETTLE_S)
    return sensor.measure(grab())


if not os.path.isfile(SENSOR_FILE):
    raise FileNotFoundError(f'{SENSOR_FILE} is missing: run 01_wavefront_sensor.py first')
sensor = sh.ShackHartmannSensor.load(SENSOR_FILE)
info = api.imcontrol.getMirrorInfo(MIRROR)
count = info['actuators']
flat = np.asarray(api.imcontrol.getMirrorFlat(MIRROR), dtype=float)
if np.abs(flat).max() + AMPLITUDE > info['maxCommand']:
    raise ValueError(f'The flat plus {AMPLITUDE} reaches beyond maxCommand {info["maxCommand"]}')

stamp = time.strftime('%Y%m%d_%H%M%S')
columns = []
started = time.time()
api.imcontrol.flattenMirror(MIRROR)
with returningToPositions(mirrors=[MIRROR]):          # flat again, also after Stop
    for k in range(count):
        push = np.zeros(count)
        push[k] = AMPLITUDE
        plus = readAt(flat + push)
        minus = readAt(flat - push)
        columns.append(ao.influence_column(plus.vector(), minus.vector(), AMPLITUDE,
                                           plus.valid_rows(), minus.valid_rows()))
        if (k + 1) % 10 == 0 or k + 1 == count:
            print(f'Actuator {k + 1} of {count} ({time.time() - started:.0f} s)')

influence = np.stack(columns, axis=1)
calibration = ao.MirrorCalibration(influence=influence, amplitude=AMPLITUDE, n_singular=count)
singular = calibration.singular_values()
calibration.n_singular = int(N_SINGULAR if N_SINGULAR is not None
                             else (singular > 0.01 * singular[0]).sum())
print('Singular values: ' + ', '.join(f'{s:.3g}' for s in singular))
print(f'Kept: {calibration.n_singular}')

weak = np.nonzero(np.nanmax(np.abs(influence), axis=0) < 0.05 * np.nanmax(np.abs(influence)))[0]
if len(weak):
    print(f'Actuators the sensor barely sees (outside the pupil?): {weak.tolist()}')

calibration.modes = list(MODES)
calibration.mode_commands = ao.mode_commands(calibration, sh.zernike_slope_matrix(sensor.grid, MODES))
path = calibration.save(os.path.join(FOLDER, f'mirror_calibration_{stamp}.npz'))
modesPath = mirror_files.write_matrix(os.path.join(FOLDER, f'zernike_modes_{stamp}.csv'),
                                      calibration.mode_commands)
print(f'Saved {path}')
print(f'Saved {modesPath}: as the mirror\'s modesFile, with firstMode {MODES[0]}')
for j, row in zip(MODES, calibration.mode_commands):
    print(f'  1 µm RMS of {z.mode_name(j):<36s} needs a largest push of {np.abs(row).max():.3f}')
print(f'Files for the closed loop: {sorted(glob.glob(os.path.join(FOLDER, "mirror_calibration_*.npz")))[-1]}')

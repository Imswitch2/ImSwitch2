"""Closed loop -- steer the mirror until the sensor reads the reference.

Reads the sensor (01_wavefront_sensor.py), inverts the newest calibration
(02_mirror_calibration.py), and repeats: measure, ``command -= GAIN x
control x slopes``, send. It stops after ITERATIONS, or when the wavefront
error (Zernike RMS without tip and tilt) is below TARGET_RMS_UM, and prints
the error at every step. A command beyond the mirror's ``maxCommand`` stops
the loop with the last good one on the mirror.

The mirror keeps the corrected command. With SAVE_AS_FLAT it also becomes the
mirror's flat (in memory) and is written to
<ImSwitchConfig>/adaptive_optics/<MIRROR>/flat_<time>.csv -- set that as
the mirror's ``flatFile`` to start from it next time.

Mock setup: adaptive_optics_mock_setup.json (its simulated sensor sees a
fixed aberration, which the loop removes).
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

WFS_CAMERA = 'WFS'
MIRROR = 'DM'

GAIN = 0.3
LEAK = 0.0                      # pulls the command towards the flat each step
ITERATIONS = 20
TARGET_RMS_UM = 0.01            # stop below this (None: run all iterations)
SETTLE_S = 0.05
FRAMES = 1
MODES = z.modes_up_to(21)       # for the printed error
SAVE_AS_FLAT = False

FOLDER = os.path.join(dirtools.UserFileDirs.Root, 'adaptive_optics', MIRROR)
SENSOR_FILE = os.path.join(FOLDER, 'wavefront_sensor.npz')


def grab(frames=FRAMES):
    stack = np.asarray(api.imcontrol.recordFramesToMemory(WFS_CAMERA, frames + 1), dtype=float)
    return stack[1:].mean(axis=0)


def error(reading):
    fit = sh.fit_zernike(reading, sensor.grid, MODES)
    return float(np.sqrt(sum(c ** 2 for j, c in fit.items() if j > 3))), fit


if not os.path.isfile(SENSOR_FILE):
    raise FileNotFoundError(f'{SENSOR_FILE} is missing: run 01_wavefront_sensor.py first')
calibrations = sorted(glob.glob(os.path.join(FOLDER, 'mirror_calibration_*.npz')))
if not calibrations:
    raise FileNotFoundError(f'No mirror_calibration_*.npz in {FOLDER}: '
                            'run 02_mirror_calibration.py first')
sensor = sh.ShackHartmannSensor.load(SENSOR_FILE)
calibration = ao.MirrorCalibration.load(calibrations[-1])
print(f'Calibration: {os.path.basename(calibrations[-1])}')
info = api.imcontrol.getMirrorInfo(MIRROR)
if info['actuators'] != calibration.actuators:
    raise ValueError(f'{MIRROR} has {info["actuators"]} actuators, the calibration '
                     f'{calibration.actuators}')

flat = np.asarray(api.imcontrol.getMirrorFlat(MIRROR), dtype=float)
command = np.asarray(api.imcontrol.getMirrorCommand(MIRROR), dtype=float)
control, rows = None, None
for step in range(ITERATIONS + 1):
    reading = sensor.measure(grab())
    rmsError, fit = error(reading)
    worst = max((j for j in fit if j > 3), key=lambda j: abs(fit[j]))
    print(f'Step {step:2d}: {rmsError:.4f} µm RMS, {int(reading.valid.sum())} lenslets, '
          f'largest {z.mode_name(worst)} {fit[worst]:+.4f}')
    if step == ITERATIONS or (TARGET_RMS_UM is not None and rmsError < TARGET_RMS_UM):
        break
    valid = reading.valid_rows()
    if rows is None or not np.array_equal(rows, valid):
        rows, control = valid, calibration.control_matrix(valid)
    proposed = ao.integrator_step(command, reading.vector(), control, gain=GAIN, leak=LEAK,
                                  flat=flat)
    if np.abs(proposed).max() > info['maxCommand']:
        print(f'Stopped: the next command reaches {np.abs(proposed).max():.3f}, beyond '
              f'maxCommand {info["maxCommand"]}; the mirror keeps the last one.')
        break
    command = np.asarray(api.imcontrol.setMirrorCommand(MIRROR, proposed.tolist()), dtype=float)
    time.sleep(SETTLE_S)

if SAVE_AS_FLAT:
    api.imcontrol.setMirrorFlat(MIRROR)
    path = api.imcontrol.saveMirrorCommand(
        MIRROR, os.path.join(FOLDER, f'flat_{time.strftime("%Y%m%d_%H%M%S")}.csv'))
    print(f'The corrected command is the flat now, saved to {path} (the mirror\'s flatFile)')

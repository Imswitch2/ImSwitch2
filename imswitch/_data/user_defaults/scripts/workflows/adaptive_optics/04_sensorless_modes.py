"""Sensorless correction -- scan each mode, keep the amplitude that sharpens the image.

The old GUI's "Run AO": for every mode in MODES, the mirror plays the
current command plus each amplitude in AMPLITUDES of that mode, the imaging
camera takes an image at each, and an image metric (METRIC) scores it. A
parabola through the scores gives the best amplitude, which is added to the
command before the next mode. A mode whose scores have no maximum inside the
scanned range keeps its amplitude at zero, and the reason is printed.

The modes are the mirror's mode matrix (its ``modesFile``): the matrix
02_mirror_calibration.py wrote (Zernike µm RMS), ALPAO's Z2C file, or the
old GUI's modal control matrix with ``modeNumbers``. A mirror without one
uses the Zernike modes of the newest calibration in
<ImSwitchConfig>/adaptive_optics/<MIRROR>/.

The images (ACQUIRE):
  * 'scan' -- the Expert panel's scan, run once per image (the rig's camera
    is triggered by the Nucleo; load e.g. the live Wide Field there first,
    with Repeat off). The frames it triggers are averaged.
  * 'frames' -- a free-running camera: FRAMES new frames, averaged.

The results -- every mode's amplitudes and scores, the amplitude kept --
go to <ImSwitchConfig>/adaptive_optics/<MIRROR>/sensorless_<time>/, with the
final command. The mirror keeps it.

Mock setup: adaptive_optics_mock_setup.json, with ACQUIRE = 'frames'. Its
mock camera does not see the mirror: what the scan keeps there is noise.
"""
# ruff: noqa: F821
import csv
import glob
import os
import time

import numpy as np

from imswitch.imcommon.algorithms import adaptive_optics as ao
from imswitch.imcommon.model import dirtools

CAMERA = 'Hamamatsu'
MIRROR = 'DM'

MODES = [4, 5, 6, 7, 8, 11]             # mode numbers of the mirror's matrix
AMPLITUDES = np.linspace(-0.1, 0.1, 7)  # in the matrix's units (µm RMS for the calibration's)
METRIC = 'highpass'                     # max, sum, highpass, snr, or band (one frequency band)
# The spectrum metrics' scale, the old GUI's constants: NA, wavelength and
# pixel at the sample.
NA, WAVELENGTH_UM, PIXEL_UM = 1.4, 0.5, 0.081
LOW, HIGH = 0.1, 0.6                    # fractions of NA / wavelength
ACQUIRE = 'scan'                        # or 'frames'
FRAMES = 1                              # for 'frames'
SETTLE_S = 0.02

MIRROR_FOLDER = os.path.join(dirtools.UserFileDirs.Root, 'adaptive_optics', MIRROR)
FOLDER = os.path.join(MIRROR_FOLDER, f'sensorless_{time.strftime("%Y%m%d_%H%M%S")}')


def image():
    if ACQUIRE == 'scan':
        stack = np.asarray(api.imcontrol.recordScanToMemory(CAMERA), dtype=float)
    elif ACQUIRE == 'frames':
        stack = np.asarray(api.imcontrol.recordFramesToMemory(CAMERA, FRAMES + 1),
                           dtype=float)[1:]
    else:
        raise ValueError(f"ACQUIRE is 'scan' or 'frames', not {ACQUIRE!r}")
    return stack.reshape((-1,) + stack.shape[-2:]).mean(axis=0)


def score(command):
    api.imcontrol.setMirrorCommand(MIRROR, [float(v) for v in command])
    time.sleep(SETTLE_S)
    return ao.image_metric(image(), METRIC, na=NA, wavelength_um=WAVELENGTH_UM,
                           pixel_um=PIXEL_UM, low=LOW, high=HIGH)


info = api.imcontrol.getMirrorInfo(MIRROR)
matrix, numbers = api.imcontrol.getMirrorModeMatrix(MIRROR), info['modes']
if matrix is None:
    calibrations = sorted(glob.glob(os.path.join(MIRROR_FOLDER, 'mirror_calibration_*.npz')))
    calibration = ao.MirrorCalibration.load(calibrations[-1]) if calibrations else None
    if calibration is None or calibration.mode_commands is None:
        raise RuntimeError(f'{MIRROR} has no mode matrix (modesFile) and there is no '
                           f'calibration in {MIRROR_FOLDER}: run 02_mirror_calibration.py')
    matrix, numbers = calibration.mode_commands, calibration.modes
    print(f'Modes: Zernike µm RMS from {os.path.basename(calibrations[-1])}')
matrix = np.asarray(matrix, dtype=float)
rowOf = {mode: row for row, mode in enumerate(numbers)}
missing = [m for m in MODES if m not in rowOf]
if missing:
    raise KeyError(f'No modes {missing} (there are {numbers})')

command = np.asarray(api.imcontrol.getMirrorCommand(MIRROR), dtype=float)
os.makedirs(FOLDER, exist_ok=True)
rows, kept = [], {}
print(f'{METRIC} at the start: {score(command):.5g}')
for mode in MODES:
    shape = matrix[rowOf[mode]]
    trial = [command + a * shape for a in AMPLITUDES]
    too_far = [a for a, c in zip(AMPLITUDES, trial) if np.abs(c).max() > info['maxCommand']]
    if too_far:
        print(f'Mode {mode}: amplitudes {too_far} reach beyond maxCommand; skipped')
        continue
    scores = [score(c) for c in trial]
    rows += [(mode, float(a), s) for a, s in zip(AMPLITUDES, scores)]
    fit = ao.quadratic_peak(AMPLITUDES, scores)
    if fit.peak is None:
        print(f'Mode {mode}: kept at 0 -- {fit.reason}')
        kept[mode] = 0.0
    else:
        command = command + fit.peak * shape
        kept[mode] = fit.peak
        print(f'Mode {mode}: {fit.peak:+.4f}')
    api.imcontrol.setMirrorCommand(MIRROR, command.tolist())
print(f'{METRIC} at the end: {score(command):.5g}')

with open(os.path.join(FOLDER, 'scores.csv'), 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['mode', 'amplitude', METRIC])
    writer.writerows(rows)
with open(os.path.join(FOLDER, 'kept.csv'), 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['mode', 'amplitude'])
    writer.writerows(kept.items())
api.imcontrol.saveMirrorCommand(MIRROR, os.path.join(FOLDER, 'command.csv'))
print(f'Results in {FOLDER}')

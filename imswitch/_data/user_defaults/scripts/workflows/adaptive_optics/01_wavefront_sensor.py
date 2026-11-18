"""Wavefront sensor -- find the lenslet grid, take the reference, read the wavefront.

The Shack-Hartmann sensor's camera looks at the lenslet spots. With the
mirror flat, this script finds the lenslet grid in a frame (or builds it
from the old GUI's typed-in values, OLD_GUI_GRID), takes the reference the
closed loop will steer back to, and saves the sensor to
<ImSwitchConfig>/adaptive_optics/<MIRROR>/wavefront_sensor.npz, where the
calibration and closed-loop scripts read it. Then it prints the wavefront
it reads now, in Zernike µm RMS (Noll; the bead-PSF fit's basis).

The reference (REFERENCE):
  * 'frame' -- this frame is the target: run it with the wavefront you want
    to hold (a reference source, or the beam after a sensorless correction).
    The old GUI's "Set base" did this.
  * 'lattice' -- the fitted lenslet lattice is the target: what a plane wave
    makes on an ideal lenslet array. Absolute, up to the array's own errors;
    the tip, tilt and defocus the lattice fit absorbs are not seen.

Before the first run
  * The sensor's camera free-running (its live view need not be on).
  * The camera pixel, lenslet pitch and focal length below (the rig: 3.45 µm
    pixels behind a Thorlabs MLA150-5C, 150 µm pitch, f = 5.2 mm).

Mock setup: adaptive_optics_mock_setup.json -- a mock ALPAO DM69 and a mock
TIS camera showing a simulated sensor behind it.
"""
# ruff: noqa: F821
import os
import time

import numpy as np

from imswitch.imcommon.algorithms import shack_hartmann as sh
from imswitch.imcommon.algorithms import zernike as z
from imswitch.imcommon.model import dirtools

# The setup's names -- change them for yours.
WFS_CAMERA = 'WFS'
MIRROR = 'DM'

PIXEL_UM = 3.45                 # the sensor camera's pixel
LENSLET_PITCH_UM = 150.0        # MLA150-5C
FOCAL_LENGTH_UM = 5200.0        # MLA150-5C
METHOD = 'centroid'             # or 'correlation' (the old GUI's default)
BACKGROUND = 0.1                # the old GUI's background factor (x Otsu)
REFERENCE = 'frame'             # or 'lattice', see above
# The old GUI's grid instead of finding it, e.g. from its AO panel's values:
# dict(center_yx=(1039, 1074), pitch_px=47, rows=34, columns=34, window=16)
OLD_GUI_GRID = None
FRAMES = 3                      # averaged per reading
MODES = z.modes_up_to(15)       # Noll 2-15 in the printout

FOLDER = os.path.join(dirtools.UserFileDirs.Root, 'adaptive_optics', MIRROR)
SENSOR_FILE = os.path.join(FOLDER, 'wavefront_sensor.npz')


def grab(frames=FRAMES):
    """The mean of ``frames`` new frames (one more is taken and dropped: it
    may have started before the mirror moved)."""
    stack = np.asarray(api.imcontrol.recordFramesToMemory(WFS_CAMERA, frames + 1), dtype=float)
    return stack[1:].mean(axis=0)


api.imcontrol.flattenMirror(MIRROR)
time.sleep(0.05)
frame = grab()

if OLD_GUI_GRID:
    grid = sh.regular_grid(**OLD_GUI_GRID)
else:
    grid = sh.find_grid(frame, LENSLET_PITCH_UM / PIXEL_UM, background=BACKGROUND)
print(f'Lenslet grid: {grid.count} lenslets, pitch {grid.pitch[0]:.2f} x {grid.pitch[1]:.2f} px, '
      f'pupil radius {grid.pupil_radius:.1f} px at (y, x) = '
      f'({grid.pupil_center[0]:.1f}, {grid.pupil_center[1]:.1f})')

sensor = sh.ShackHartmannSensor(grid=grid, pixel_um=PIXEL_UM, focal_length_um=FOCAL_LENGTH_UM,
                                method=METHOD, background=BACKGROUND)
if REFERENCE == 'lattice':
    sensor.set_lattice_reference(frame)
elif REFERENCE == 'frame':
    sensor.set_reference(frame)
else:
    raise ValueError(f"REFERENCE is 'frame' or 'lattice', not {REFERENCE!r}")
seen = int(np.all(np.isfinite(sensor.reference_positions), axis=1).sum())
print(f'Reference ({REFERENCE}): {seen} of {grid.count} spots seen')
print(f'Saved the sensor to {sensor.save(SENSOR_FILE)}')

reading = sensor.measure(grab())
fit = sh.fit_zernike(reading, grid, MODES)
total = float(np.sqrt(sum(c ** 2 for j, c in fit.items() if j > 3)))
print(f'Wavefront now ({int(reading.valid.sum())} lenslets), µm RMS against the reference:')
for j, c in fit.items():
    print(f'  {z.mode_name(j):<40s} {c:+.4f}')
print(f'  RMS without tip and tilt: {total:.4f} µm')

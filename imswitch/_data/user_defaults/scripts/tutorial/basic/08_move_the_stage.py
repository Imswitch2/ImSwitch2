"""Tutorial basic 08 -- Move the stage and snap a small grid.

You will learn
  * ``getPositionerNames()`` / ``getPositionerPositions()``: what can move,
    and where it is
  * ``setPositioner()`` (absolute) and ``movePositioner()`` (relative)
  * ``waitUntil()``: wait for the stage to arrive before taking an image
  * to save your own results with tifffile

Setup
  Mock setup:   example_no_hardware.json
  It simulates: two cameras and three stages -- "Mock X" and "Mock Y" (one
                axis each) and "Mock Kinesis XY" (two axes). The stages
                arrive at once and report their position.
  Your own microscope: needs an X and a Y stage, a camera, and the
                Positioner and Recording widgets. Change X_STAGE and
                Y_STAGE to your stage names.

Next: the scanning tutorials, starting with ../scanning/01_run_a_scan.py
"""

import os

import numpy as np
import tifffile

X_STAGE, Y_STAGE = 'Mock X', 'Mock Y'    # names from getPositionerNames()
STEP_UM = 50.0                           # stage positions are in µm

print('Stages:', api.imcontrol.getPositionerNames())
# A dictionary of dictionaries, {stage name: {axis: position}}, because a
# stage can have several axes: "Mock Kinesis XY" has both X and Y.
print('Positions:', api.imcontrol.getPositionerPositions())


def position(stage, axis):
    """Where one axis of one stage is now, in µm."""
    return api.imcontrol.getPositionerPositions()[stage][axis]


def moveTo(stage, axis, target):
    """Move an axis to `target` and wait until the stage reports it there.

    A simulated stage is there at once; a real one takes time, and an image
    taken on the way is blurred or in the wrong place. The 0.5 µm tolerance
    allows for a stage that stops just short of the target.
    """
    api.imcontrol.setPositioner(stage, axis, target)
    waitUntil(lambda: abs(position(stage, axis) - target) < 0.5, timeout=10)


camera = api.imcontrol.getDetectorNames()[0]
api.imcontrol.setDetectorToRecord(camera)
startX, startY = position(X_STAGE, 'X'), position(Y_STAGE, 'Y')

# movePositioner() moves *by* a distance; setPositioner() moves *to* a
# position. Here: 10 µm to the right, then back to the start.
api.imcontrol.movePositioner(X_STAGE, 'X', 10)
print(f'X after a +10 µm move: {position(X_STAGE, "X")}')
moveTo(X_STAGE, 'X', startX)

# A 3 x 3 grid: for each row, move Y; for each column in it, move X; snap.
tiles = []
try:
    for row in range(3):
        for column in range(3):
            moveTo(Y_STAGE, 'Y', startY + row * STEP_UM)
            moveTo(X_STAGE, 'X', startX + column * STEP_UM)
            tiles.append(api.imcontrol.snapImage(True)[camera])
            print(f'tile ({row}, {column}) at X={position(X_STAGE, "X")}, '
                  f'Y={position(Y_STAGE, "Y")}')
finally:
    moveTo(X_STAGE, 'X', startX)          # back to where we started,
    moveTo(Y_STAGE, 'Y', startY)          # even if you press Stop

# Results you compute yourself can be saved anywhere; next to the
# recordings is handy. np.stack turns the 9 images into one (9, rows,
# columns) array, and tifffile writes it as a 9-page TIFF.
folder = api.imcontrol.getRecFolder()
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, 'tutorial_grid.tif')
tifffile.imwrite(path, np.stack(tiles))
print(f'Saved {len(tiles)} tiles to', path)

"""Tutorial basic 06 -- Move the stage and snap a small grid.

You will learn
  * ``getPositionerNames()`` / ``getPositionerPositions()``: what can move,
    and where it is
  * ``setPositioner()`` (absolute) and ``movePositioner()`` (relative)
  * ``waitUntil()``: wait for the stage to arrive before taking an image
  * to save your own results with tifffile

Setup
  Mock setup: example_no_hardware.json
  Needs:      an X and a Y stage, a camera, and the Positioner and
              Recording widgets. Edit X_STAGE / Y_STAGE for your setup.

Next: 07_record_until_stop_safely.py
"""

import os

import numpy as np
import tifffile

X_STAGE, Y_STAGE = 'Mock X', 'Mock Y'    # names from getPositionerNames()
STEP_UM = 50.0                           # stage units are µm

print('Stages:', api.imcontrol.getPositionerNames())
# {stage name: {axis: position}} -- a stage can have several axes.
print('Positions:', api.imcontrol.getPositionerPositions())


def position(stage, axis):
    return api.imcontrol.getPositionerPositions()[stage][axis]


def moveTo(stage, axis, target):
    """Move and wait until the stage reports the target. A mock stage is
    there at once; a real one takes time, and a snap taken on the way is
    blurred or in the wrong place."""
    api.imcontrol.setPositioner(stage, axis, target)
    waitUntil(lambda: abs(position(stage, axis) - target) < 0.5, timeout=10)


camera = api.imcontrol.getDetectorNames()[0]
api.imcontrol.setDetectorToRecord(camera)
startX, startY = position(X_STAGE, 'X'), position(Y_STAGE, 'Y')

# Relative move: 10 µm to the right and back again.
api.imcontrol.movePositioner(X_STAGE, 'X', 10)
print(f'X after a +10 µm move: {position(X_STAGE, "X")}')
moveTo(X_STAGE, 'X', startX)

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
    moveTo(X_STAGE, 'X', startX)          # back to where we started
    moveTo(Y_STAGE, 'Y', startY)

# Your own results can go anywhere; next to the recordings is handy.
folder = api.imcontrol.getRecFolder()
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, 'tutorial_grid.tif')
tifffile.imwrite(path, np.stack(tiles))
print(f'Saved {len(tiles)} tiles to', path)

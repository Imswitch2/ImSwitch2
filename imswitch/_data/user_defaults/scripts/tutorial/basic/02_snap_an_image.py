"""Tutorial basic 02 -- Snap an image and look at its pixels.

You will learn
  * ``snapImage(True)``: take an image and get it back as a numpy array
  * ``snapImage()``: take an image and save it to a file, like the Snap button
  * where snapped files go, and how to save them as TIFF

Setup
  Mock setup: example_mock.json
  Needs:      a camera and the Recording widget.

Next: 03_camera_settings.py
"""

import os

import numpy as np

camera = api.imcontrol.getDetectorNames()[0]

# snapImage(True) returns {detector name: image}. It snaps the detectors the
# Recording widget is set to record -- by default the one in the image view.
# Tutorial 05 shows how to choose them.
images = api.imcontrol.snapImage(True)
image = images[camera]
print(f'{camera}: {image.shape[1]} x {image.shape[0]} pixels, {image.dtype}')
print(f'  mean {image.mean():.1f}, min {image.min()}, max {image.max()}')

# It is an ordinary numpy array: anything numpy can do, you can do here.
brightest = np.unravel_index(np.argmax(image), image.shape)
print(f'  brightest pixel at row {brightest[0]}, column {brightest[1]}')

# snapImage() without True saves the snap instead, into the recording folder
# (the one shown in the Recording widget) and returns nothing. The format is
# the widget's "Snap format": HDF5 unless you pick TIFF or ZARR.
folder = api.imcontrol.getRecFolder()


def newFiles(before):
    return sorted(set(os.listdir(folder)) - before) if os.path.isdir(folder) else []


for fileFormat in ('HDF5', 'TIFF'):
    api.imcontrol.setSnapModeSave(fileFormat)
    before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    api.imcontrol.snapImage()
    waitUntil(lambda: newFiles(before), timeout=10)
    for name in newFiles(before):
        print(f'Saved as {fileFormat}:', os.path.join(folder, name))

# The format stays selected in the Recording widget after the script; put
# the default back so the Snap button behaves as before.
api.imcontrol.setSnapModeSave('HDF5')

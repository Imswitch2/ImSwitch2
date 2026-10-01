"""Tutorial basic 02 -- Snap an image and look at its pixels.

You will learn
  * ``snapImage(True)``: take an image and get it back as a numpy array
  * ``snapImage()``: take an image and save it to a file, like the Snap button
  * where snapped files go, and how to save them as TIFF

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera", 800 x 800 pixels) that returns noise
                images.
  Your own microscope: needs a camera and the Recording widget.

Next: 03_camera_settings.py
"""

import os

import numpy as np

# getDetectorNames() returns a list; [0] takes its first entry -- here the
# only camera, called "Camera".
camera = api.imcontrol.getDetectorNames()[0]

# snapImage(True) takes one image per selected camera and returns them as a
# dictionary {camera name: image}, without saving anything. Which cameras
# are selected is set in the Recording widget -- by default the one shown
# in the image view. Tutorial 07 shows how to choose them from a script.
images = api.imcontrol.snapImage(True)
image = images[camera]            # look up this camera's image by its name

# The image is a numpy array. shape is (rows, columns) -- height first --
# and dtype is the pixel type: uint16 means whole numbers from 0 to 65535.
print(f'{camera}: {image.shape[1]} x {image.shape[0]} pixels, {image.dtype}')
print(f'  mean {image.mean():.1f}, min {image.min()}, max {image.max()}')

# Anything numpy can do, you can do here. argmax finds the brightest pixel
# (as a position in the flattened array); unravel_index turns that back into
# a (row, column) pair.
brightest = np.unravel_index(np.argmax(image), image.shape)
print(f'  brightest pixel at row {brightest[0]}, column {brightest[1]}')

# snapImage() without True saves the snap instead, like the Snap button, and
# returns nothing. It goes into the recording folder -- the one shown in the
# Recording widget -- in the widget's "Snap format": HDF5, TIFF or ZARR.
folder = api.imcontrol.getRecFolder()


def newFiles(before):
    """The file names in the recording folder that are not in `before`."""
    if not os.path.isdir(folder):
        return []
    return sorted(set(os.listdir(folder)) - before)


for fileFormat in ('HDF5', 'TIFF'):
    api.imcontrol.setSnapModeSave(fileFormat)        # choose the snap format
    before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    api.imcontrol.snapImage()
    # waitUntil() checks the condition again and again until it is true, or
    # gives up with an error after `timeout` seconds. `lambda: ...` is a
    # small function written in place: the condition to check.
    waitUntil(lambda: newFiles(before), timeout=10)
    for name in newFiles(before):
        print(f'Saved as {fileFormat}:', os.path.join(folder, name))

# The chosen format stays selected in the Recording widget after the
# script. Put the default back so the Snap button behaves as before.
api.imcontrol.setSnapModeSave('HDF5')

"""Tutorial basic 10 -- Frames as an array, without a file.

You will learn
  * ``recordFramesToMemory()``: record a number of frames and get them
    back as one NumPy array -- nothing written
  * ``getLatestFrame()``: the newest frame of the live view, without a snap
  * to work on the frames in the script: a mean image and the noise per pixel

Setup
  Mock setup:   example_no_hardware.json
  It simulates: two cameras ("Mock Camera" and "Mock ThorCam TSI") that
                return noise images, three stages and three rotation mounts.
  Your own microscope: needs a camera and the Recording and View widgets.
                Change CAMERA to your camera's name.

Next: 11_work_interactively.py
"""

import os

import numpy as np

CAMERA = 'Mock Camera'                   # a name from getDetectorNames()
FRAMES = 20

# Nothing is written: the files in the recording folder stay as they are.
folder = api.imcontrol.getRecFolder()
filesBefore = set(os.listdir(folder)) if os.path.isdir(folder) else set()

# One call: it records FRAMES frames to memory, waits for the end, and
# returns them as (frames, rows, columns). Tutorial 04 did the same through
# a file; this is for frames the script only needs to look at.
frames = api.imcontrol.recordFramesToMemory(CAMERA, FRAMES, timeout=60)
print(f'{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px, '
      f'{frames.dtype}, {frames.nbytes / 1e6:.1f} MB in memory')
assert frames.shape[0] == FRAMES
assert (set(os.listdir(folder)) if os.path.isdir(folder) else set()) == filesBefore

# The frames are a numpy array: the mean image, and how much each pixel
# varies from frame to frame -- the noise, which a single snap cannot show.
mean = frames.mean(axis=0)
noise = frames.std(axis=0)
print(f'mean image: {mean.min():.1f} to {mean.max():.1f} counts; '
      f'noise per pixel: {noise.mean():.2f} counts on average')

# getLatestFrame() is lighter still: the newest frame the camera delivered,
# as the live view shows it, with no recording and no snap. The camera must
# be acquiring for the frame to be new -- here the live view is turned on.
api.imcontrol.setLiveViewActive(True)
try:
    sleep(0.5)                                       # let a frame or two arrive
    live = api.imcontrol.getLatestFrame(CAMERA)
    print(f'live frame: {live.shape[1]} x {live.shape[0]} px, mean {live.mean():.1f}')
    assert live.shape == frames.shape[1:]
finally:
    api.imcontrol.setLiveViewActive(False)

# A loop that reads getLatestFrame() and moves a stage until the image is
# sharpest is an autofocus; one that reads it between laser settings is a
# power calibration. Both are a few lines from here.

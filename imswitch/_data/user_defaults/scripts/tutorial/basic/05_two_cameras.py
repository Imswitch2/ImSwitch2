"""Tutorial basic 05 -- Two cameras: one after the other, then together.

You will learn
  * ``setDetectorToRecord()``: which detectors a snap or recording uses
  * to snap each camera in turn, and both in one go
  * to record both cameras, into one file or one file each

Setup
  Mock setup: example_no_hardware.json
  Needs:      two cameras and the Recording widget.

Next: 06_move_the_stage.py
"""

import glob
import os

import h5py

cameras = api.imcontrol.getDetectorNames()
print('Cameras:', cameras)
signals = api.imcontrol.signals()
folder = api.imcontrol.getRecFolder()
previousFormat = api.imcontrol.getRecFileFormat()

try:
    # One after the other: select a camera, snap, select the next.
    for camera in cameras:
        api.imcontrol.setDetectorToRecord(camera)
        image = api.imcontrol.snapImage(True)[camera]
        print(f'{camera}: {image.shape[1]} x {image.shape[0]} px, mean {image.mean():.1f}')

    # Together: a list selects several. The snap returns one image each.
    api.imcontrol.setDetectorToRecord(cameras)
    images = api.imcontrol.snapImage(True)
    print('One snap, images from:', sorted(images))

    # Recording several cameras writes one file per camera -- or, with
    # multiDetectorSingleFile=True, a single file with a group per camera.
    api.imcontrol.setDetectorToRecord(cameras, multiDetectorSingleFile=True)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setRecModeSpecFrames(5)
    api.imcontrol.setRecFilename('tutorial_two_cameras')
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=60)
finally:
    # -1 means "the camera shown in the image view", the default.
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setRecFileFormat(previousFormat)

path = max(glob.glob(os.path.join(folder, 'tutorial_two_cameras*.hdf5')),
           key=os.path.getmtime)
print('Recorded', path)
with h5py.File(path, 'r') as f:
    for camera in cameras:
        print(f'  {camera}: {f[camera]["data"].shape[0]} frames')

"""Tutorial basic 07 -- Two cameras: one after the other, then together.

You will learn
  * ``setDetectorToRecord()``: which detectors a snap or recording uses
  * to snap each camera in turn, and both in one go
  * to record both cameras, into one file or one file each

Setup
  Mock setup:   example_no_hardware.json   <- a different setup from 01-06
  It simulates: two cameras -- "Mock Camera" (800 x 800 pixels) and
                "Mock ThorCam TSI" (2448 x 2048 pixels) -- and three stages.
  Your own microscope: needs two cameras and the Recording widget.

Next: 08_move_the_stage.py
"""

import glob
import os

import h5py

cameras = api.imcontrol.getDetectorNames()
print('Cameras:', cameras)
signals = api.imcontrol.signals()
folder = api.imcontrol.getRecFolder()
previousFormat = api.imcontrol.getRecFileFormat()
helpers = importScript('tutorial_helpers.py')     # from tutorial 06

# Each camera gets the settings the recording below relies on (tutorial 04
# explains why). Different cameras name them differently: the Thorlabs one
# has its exposure in µs, and an 'Operation Mode' that must be 'Software' --
# 'Hardware' would make it wait for trigger pulses this setup never sends.
CAMERA_SETTINGS = {
    'Mock Camera': {'exposure': 20},                                  # ms
    'Mock ThorCam TSI': {'Exposure': 20000, 'Operation Mode': 'Software'},  # µs
}
previousSettings = {camera: helpers.applyCameraSettings(camera, settings)
                    for camera, settings in CAMERA_SETTINGS.items()}

try:
    # One after the other: select one camera, snap, then the next. With a
    # single name, snapImage(True) returns a dictionary with one entry.
    for camera in cameras:
        api.imcontrol.setDetectorToRecord(camera)
        image = api.imcontrol.snapImage(True)[camera]
        print(f'{camera}: {image.shape[1]} x {image.shape[0]} px, mean {image.mean():.1f}')

    # Together: a list of names selects several cameras at once, and one
    # snap returns an image from each.
    api.imcontrol.setDetectorToRecord(cameras)
    images = api.imcontrol.snapImage(True)
    print('One snap, images from:', sorted(images))

    # Recording several cameras writes one file per camera, or -- with
    # multiDetectorSingleFile=True -- one file with a group per camera.
    api.imcontrol.setDetectorToRecord(cameras, multiDetectorSingleFile=True)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setRecModeSpecFrames(5)
    api.imcontrol.setRecFilename('tutorial_two_cameras')
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=60)
finally:
    # -1 means "the camera shown in the image view", ImSwitch's default.
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setRecFileFormat(previousFormat)
    for camera, settings in previousSettings.items():
        helpers.applyCameraSettings(camera, settings)

# One file for both cameras: <name>_rec.hdf5, with a group per camera.
path = max(glob.glob(os.path.join(folder, 'tutorial_two_cameras*.hdf5')),
           key=os.path.getmtime)
print('Recorded', path)
with h5py.File(path, 'r') as f:
    for camera in cameras:
        print(f'  {camera}: {f[camera]["data"].shape[0]} frames')

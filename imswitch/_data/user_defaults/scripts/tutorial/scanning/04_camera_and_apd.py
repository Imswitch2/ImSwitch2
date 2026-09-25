"""Tutorial scanning 04 -- A camera and a point detector in the same scan.

You will learn
  * that detectors record a scan differently: a camera stores one frame
    per trigger pulse, a point detector (APD, PMT) one image assembled from
    all positions
  * to record several detectors from one scan

Setup
  Mock setup:   mixed_hamamatsu_apd_mock_scan_setup.json   <- a new setup
  It simulates: the setup of tutorials 01-03 -- a Hamamatsu camera
                ("Camera") triggered by the scan, X/Y/Z stages, an NI-DAQ
                card -- plus an APD ("APD"), a detector that counts photons
                at one point and gives one value per scan position.
  Your own microscope: needs a Base Scan widget with X/Y stages, a camera
                the scan can trigger, a point detector, and the Recording
                widget.

Next: 05_laser_power_series.py (on a different mock setup)
"""

import glob
import os

import h5py

# The names of this setup's devices -- change them for yours (see 01).
CAMERA = 'Camera'
STAGES = ['X', 'Y', 'Z']
# Trigger mode and exposure the scan needs from the camera (see 01).
CAMERA_SETTINGS = {'Trigger source': 'External "frame-trigger"',
                   'Set exposure time': 0.005}                  # s

# The steps of tutorial 01 -- load the scan settings with these names, save
# and restore the Scan widget, set and restore the camera -- as functions.
scan = importScript('scan_helpers.py')
APD = 'APD'                            # the point detector
NAME = 'tutorial_camera_and_apd'
signals = api.imcontrol.signals()

backup = scan.backupScanSettings()
previousCamera = scan.applyCameraSettings(CAMERA, CAMERA_SETTINGS)
previousFormat = api.imcontrol.getRecFileFormat()
try:
    # Only the camera takes trigger pulses: the APD is read out by the scan
    # itself, one value per position.
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setDetectorToRecord([CAMERA, APD])   # both detectors
    api.imcontrol.setRecModeScanOnce()
    api.imcontrol.setRecFilename(NAME)
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=120)
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFileFormat(previousFormat)
    api.imcontrol.loadScanParamsFromFile(backup)
    scan.applyCameraSettings(CAMERA, previousCamera)

# Each detector has its own file; the data shapes show the difference.
folder = api.imcontrol.getRecFolder()
for detector in (CAMERA, APD):
    path = max(glob.glob(os.path.join(folder, f'{NAME}_rec_{detector}*')),
               key=os.path.getmtime)
    with h5py.File(path, 'r') as f:
        print(f'{detector}: {f[detector + "/data"].shape}  <- {os.path.basename(path)}')
# Camera: (100, 512, 512) -- a whole picture at each of the 10 x 10 positions.
# APD:    (1, 1, 10, 10)  -- one scan image, one pixel per position.

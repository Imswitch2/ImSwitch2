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
import tempfile

import h5py

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')
NAME = 'tutorial_camera_and_apd'
detectors = api.imcontrol.getDetectorNames()     # ['Camera', 'APD']
signals = api.imcontrol.signals()

backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
previousFormat = api.imcontrol.getRecFileFormat()
try:
    api.imcontrol.loadScanParamsFromFile(PARAMS)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setDetectorToRecord(detectors)   # both detectors
    api.imcontrol.setRecModeScanOnce()
    api.imcontrol.setRecFilename(NAME)
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=120)
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFileFormat(previousFormat)
    api.imcontrol.loadScanParamsFromFile(backup)

# Each detector has its own file; the data shapes show the difference.
folder = api.imcontrol.getRecFolder()
for detector in detectors:
    path = max(glob.glob(os.path.join(folder, f'{NAME}_rec_{detector}*')),
               key=os.path.getmtime)
    with h5py.File(path, 'r') as f:
        print(f'{detector}: {f[detector + "/data"].shape}  <- {os.path.basename(path)}')
# Camera: (100, 512, 512) -- a whole picture at each of the 10 x 10 positions.
# APD:    (1, 1, 10, 10)  -- one scan image, one pixel per position.

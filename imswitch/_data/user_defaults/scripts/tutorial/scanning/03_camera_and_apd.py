"""Tutorial scanning 03 -- A camera and a point detector in the same scan.

You will learn
  * that detectors record a scan differently: a camera stores one frame
    per trigger, a point detector (APD, PMT) one image assembled from all
    positions
  * to record several detectors from one scan

Setup
  Mock setup: mixed_hamamatsu_apd_mock_scan_setup.json
  Needs:      a Base Scan widget with X/Y positioners, a camera it can
              trigger, an APD, and the Recording widget.

Next: 04_scan_timelapse.py
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
    api.imcontrol.setDetectorToRecord(detectors)
    api.imcontrol.setRecModeScanOnce()
    api.imcontrol.setRecFilename(NAME)
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=120)
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFileFormat(previousFormat)
    api.imcontrol.loadScanParamsFromFile(backup)

folder = api.imcontrol.getRecFolder()
for detector in detectors:
    path = max(glob.glob(os.path.join(folder, f'{NAME}_rec_{detector}*')),
               key=os.path.getmtime)
    with h5py.File(path, 'r') as f:
        print(f'{detector}: {f[detector + "/data"].shape}  <- {os.path.basename(path)}')
# Camera: (100, 512, 512) -- a frame at each of the 10 x 10 positions.
# APD:    (1, 1, 10, 10)  -- one scan image, a pixel per position.

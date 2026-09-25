"""Tutorial scanning 02 -- Record a scan: one camera frame per position.

You will learn
  * ``setRecModeScanOnce()``: a recording that runs one scan and keeps
    every frame the scan triggers
  * that the recording starts the scan itself -- no runScanAndWait()
  * to check the file: one frame per scan position

Setup
  Mock setup: hamamatsu_mock_scan_setup.json
  Needs:      as 01_run_a_scan.py, plus the Recording widget.

Next: 03_camera_and_apd.py
"""

import glob
import os
import tempfile

import h5py

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')
NAME = 'tutorial_scan'
camera = api.imcontrol.getDetectorNames()[0]
signals = api.imcontrol.signals()

backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
previousFormat = api.imcontrol.getRecFileFormat()
try:
    api.imcontrol.loadScanParamsFromFile(PARAMS)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setDetectorToRecord(camera)
    api.imcontrol.setRecModeScanOnce()
    api.imcontrol.setRecFilename(NAME)
    # recordingEnded comes once the scan is over *and* the file is written.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=120)
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFileFormat(previousFormat)
    api.imcontrol.loadScanParamsFromFile(backup)

path = max(glob.glob(os.path.join(api.imcontrol.getRecFolder(), f'{NAME}_rec_{camera}*')),
           key=os.path.getmtime)
with h5py.File(path, 'r') as f:
    frames = f[f'{camera}/data']
    print(f'{path}\n  {frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px')
# 10 x 10 positions, one trigger each: 100 frames. A frame count that does
# not match the positions means triggers were lost -- worth checking on a
# new setup before trusting a long acquisition.
assert frames.shape[0] == 100, 'expected one frame per scan position'

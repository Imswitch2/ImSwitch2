"""Tutorial scanning 02 -- Record a scan: one camera frame per position.

You will learn
  * ``setRecModeScanOnce()``: a recording that runs one scan and keeps
    every frame the scan triggers
  * that such a recording starts the scan itself -- no runScanAndWait()
  * to check the file: one frame per scan position

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) triggered by
                the scan, X/Y/Z stages, and an NI-DAQ card that runs the
                scan.
  Your own microscope: as 01_run_a_scan.py, plus the Recording widget.

Next: 03_scan_timelapse.py
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
NAME = 'tutorial_scan'
signals = api.imcontrol.signals()

# Save the Scan widget's settings, the camera's and your recording file
# format, so all can be put back afterwards (see tutorial 01 and basic 04).
backup = scan.backupScanSettings()
previousCamera = scan.applyCameraSettings(CAMERA, CAMERA_SETTINGS)
previousFormat = api.imcontrol.getRecFileFormat()
try:
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)
    api.imcontrol.setRecFileFormat('HDF5')     # read back with h5py below
    api.imcontrol.setDetectorToRecord(CAMERA)
    # "Scan once": starting the recording starts one scan, and the
    # recording keeps each frame the scan's pulses trigger.
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
    scan.applyCameraSettings(CAMERA, previousCamera)

path = max(glob.glob(os.path.join(api.imcontrol.getRecFolder(), f'{NAME}_rec_{CAMERA}*')),
           key=os.path.getmtime)
with h5py.File(path, 'r') as f:
    frames = f[f'{CAMERA}/data']               # (frames, rows, columns)
    print(f'{path}\n  {frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px')

# 10 x 10 positions, one pulse each: 100 frames. A count that does not match
# the positions means pulses were lost -- or the camera was not waiting for
# them (its trigger mode) -- worth checking on a new setup before trusting a
# long acquisition. assert stops the script with an error if the condition
# is false.
assert frames.shape[0] == 100, 'expected one frame per scan position'

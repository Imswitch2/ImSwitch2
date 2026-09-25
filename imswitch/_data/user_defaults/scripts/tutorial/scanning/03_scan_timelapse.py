"""Tutorial scanning 03 -- Repeat a scan at intervals: a scan timelapse.

You will learn
  * ``setRecModeScanTimelapse()``: N scans, one every so many seconds
  * that recordingEnded comes once, after the last scan, not after each
  * where each timepoint is saved

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) triggered by
                the scan, X/Y/Z stages, and an NI-DAQ card that runs the
                scan.
  Your own microscope: as 02_record_a_scan.py.

Next: 04_camera_and_apd.py (on a different mock setup)
"""

import glob
import os
import time

# The names of this setup's devices -- change them for yours (see 01).
CAMERA = 'Camera'
STAGES = ['X', 'Y', 'Z']
# Trigger mode and exposure the scan needs from the camera (see 01).
CAMERA_SETTINGS = {'Trigger source': 'External "frame-trigger"',
                   'Set exposure time': 0.005}                  # s

# The steps of tutorial 01 -- load the scan settings with these names, save
# and restore the Scan widget, set and restore the camera -- as functions.
scan = importScript('scan_helpers.py')
NAME = 'tutorial_timelapse'
TIMEPOINTS, INTERVAL_S = 3, 2.0        # 3 scans, one every 2 seconds
signals = api.imcontrol.signals()

backup = scan.backupScanSettings()
previousCamera = scan.applyCameraSettings(CAMERA, CAMERA_SETTINGS)
try:
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)
    # Each scan of the timelapse is saved to its own file;
    # timelapseSingleFile=True would put all timepoints into one file.
    api.imcontrol.setRecModeScanTimelapse(TIMEPOINTS, INTERVAL_S,
                                          timelapseSingleFile=False)
    api.imcontrol.setRecFilename(NAME)
    t0 = time.monotonic()
    # The timeout has to allow for every scan and every interval between
    # them; the 60 s per timepoint is room to spare, not the expected time.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=TIMEPOINTS * (INTERVAL_S + 60))
    print(f'{TIMEPOINTS} timepoints in {time.monotonic() - t0:.1f} s')
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.loadScanParamsFromFile(backup)
    scan.applyCameraSettings(CAMERA, previousCamera)

# One file per timepoint: <name>_rec_scan0_<camera>, ..._scan1_..., ...
for path in sorted(glob.glob(os.path.join(api.imcontrol.getRecFolder(), f'{NAME}*'))):
    print('  ', os.path.basename(path))

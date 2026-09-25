"""Tutorial scanning 04 -- Repeat a scan at intervals: a scan timelapse.

You will learn
  * ``setRecModeScanTimelapse()``: N scans, one every so many seconds
  * that recordingEnded comes once, after the last scan, not after each
  * where each timepoint is saved

Setup
  Mock setup: hamamatsu_mock_scan_setup.json
  Needs:      as 02_record_a_scan.py.

Next: 05_laser_power_series.py
"""

import glob
import os
import tempfile
import time

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')
NAME = 'tutorial_timelapse'
TIMEPOINTS, INTERVAL_S = 3, 2.0
signals = api.imcontrol.signals()

backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
try:
    api.imcontrol.loadScanParamsFromFile(PARAMS)
    # timelapseSingleFile=True would put all timepoints into one file.
    api.imcontrol.setRecModeScanTimelapse(TIMEPOINTS, INTERVAL_S,
                                          timelapseSingleFile=False)
    api.imcontrol.setRecFilename(NAME)
    t0 = time.monotonic()
    # Allow for every scan and every interval, with room to spare.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=TIMEPOINTS * (INTERVAL_S + 60))
    print(f'{TIMEPOINTS} timepoints in {time.monotonic() - t0:.1f} s')
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.loadScanParamsFromFile(backup)

for path in sorted(glob.glob(os.path.join(api.imcontrol.getRecFolder(), f'{NAME}*'))):
    print('  ', os.path.basename(path))

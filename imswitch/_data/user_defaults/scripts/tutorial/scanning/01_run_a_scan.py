"""Tutorial scanning 01 -- Run a scan that triggers the camera.

You will learn
  * how a scan drives a camera: the stages step through the positions and
    the camera gets one trigger (TTL) pulse per position
  * ``loadScanParamsFromFile()`` / ``saveScanParamsToFile()``: scan settings
    as files -- design them in the Scan widget, save, load in a script
  * ``runScanAndWait()``: run one scan and wait for exactly that scan
  * the scan signals, and how to watch them

Setup
  Mock setup: hamamatsu_mock_scan_setup.json
  Needs:      a Scan widget of the Base type with X/Y positioners, and a
              camera it can trigger (a camera with a digitalLine).

  scan_params/camera_scan_1um.json is loaded from this folder.

Next: 02_record_a_scan.py
"""

import json
import os
import tempfile
import time

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')

# A setup can have several scan widgets; runScanAndWait(source=...) picks one.
print('Scan sources:', api.imcontrol.getScanSourceNames())

# A scan file is what the Scan widget shows, as JSON. This one moves X and Y
# over 1 µm in 0.1 µm steps -- 10 x 10 positions, 10 ms each -- and pulses
# the camera's line from 0 to 5 ms of every step.
params = json.load(open(PARAMS))
analog, digital = params['analogParameterDict'], params['digitalParameterDict']
print('Axes:', analog['target_device'], 'lengths (µm):', analog['axis_length'],
      'steps (µm):', analog['axis_step_size'])
print('Triggered:', digital['target_device'], 'from', digital['TTL_start'],
      'to', digital['TTL_end'], 's of each', digital['sequence_time'], 's step')

# Keep what the Scan widget had, to put it back afterwards.
backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)

signals = api.imcontrol.signals()

try:
    api.imcontrol.loadScanParamsFromFile(PARAMS)
    mainWindow.setCurrentModule('imcontrol')   # watch the Scan widget

    # To know that something happened, create a waiter *before* causing it.
    # (A function you connect to a signal yourself runs on the script's
    # thread, and only while the script waits in getWaitForSignal() or
    # callAndWaitForSignal() -- so use those rather than signal.connect.)
    scanStarted = getWaitForSignal(signals.scanStarted, timeout=10)

    t0 = time.monotonic()
    # runScanAndWait starts the scan and returns when it has ended. It raises
    # if the scan was refused (say, too long for the setup's maxScanTimeMin)
    # or failed, and returns at once on Stop.
    runScanAndWait(timeout=120)
    scanStarted()                              # already emitted: returns at once
    print(f'Scan started and done in {time.monotonic() - t0:.1f} s')
finally:
    api.imcontrol.loadScanParamsFromFile(backup)
    mainWindow.setCurrentModule('imscripting')

# The camera took one frame per trigger, but nothing kept them: the live
# view shows the latest only. The next tutorial records them.

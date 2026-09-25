"""Tutorial scanning 01 -- Run a scan that triggers the camera.

You will learn
  * how a scan drives a camera: the stages step through the positions and
    the camera gets one trigger pulse (TTL) per position
  * ``loadScanParamsFromFile()`` / ``saveScanParamsToFile()``: scan settings
    as files -- design them in the Scan widget, save, load in a script
  * ``runScanAndWait()``: run one scan and wait for exactly that scan
  * ``getWaitForSignal()``: find out whether something happened

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) that takes
                a picture only when it receives a trigger pulse, X/Y/Z
                stages, and an NI-DAQ card that runs the scan and sends the
                pulses.
  Your own microscope: needs a Scan widget of the Base type with X/Y
                stages, and a camera the scan can trigger (one with a
                digitalLine in the setup file).

  scan_params/camera_scan_1um.json in this folder holds the scan settings.

Next: 02_record_a_scan.py
"""

import json
import os
import tempfile
import time

# getScriptDirPath() is this script's folder, so the settings file is found
# wherever the tutorials were copied to.
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')

# A setup can have several scan widgets; getScanSourceNames() lists them and
# runScanAndWait(source=...) picks one. This setup has one, called 'Scan'.
print('Scan sources:', api.imcontrol.getScanSourceNames())

# A scan settings file holds what the Scan widget shows, as JSON. This one
# moves X and Y over 1 µm in 0.1 µm steps -- 10 x 10 positions, 10 ms at
# each -- and sends the camera a pulse from 0 to 5 ms of every step.
params = json.load(open(PARAMS))
analog, digital = params['analogParameterDict'], params['digitalParameterDict']
print('Axes:', analog['target_device'], 'lengths (µm):', analog['axis_length'],
      'steps (µm):', analog['axis_step_size'])
print('Triggered:', digital['target_device'], 'from', digital['TTL_start'],
      'to', digital['TTL_end'], 's of each', digital['sequence_time'], 's step')

# Loading the file changes the Scan widget. Save what it shows now to a
# temporary file, so it can be put back at the end.
backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)

signals = api.imcontrol.signals()

try:
    api.imcontrol.loadScanParamsFromFile(PARAMS)
    mainWindow.setCurrentModule('imcontrol')   # watch the Scan widget

    # To find out whether something happened, create a waiter *before*
    # causing it: getWaitForSignal() starts listening now, and calling the
    # waiter later returns at once if the signal came in the meantime.
    # (A function you connect to a signal yourself runs only while the
    # script waits in getWaitForSignal() or callAndWaitForSignal() -- so
    # use those rather than signal.connect.)
    scanStarted = getWaitForSignal(signals.scanStarted, timeout=10)

    t0 = time.monotonic()                      # a clock, to time the scan
    # runScanAndWait() starts the scan and returns when it has ended. It
    # raises an error if the scan was refused -- for example, too long for
    # the setup's maxScanTimeMin -- or failed, and returns at once on Stop.
    runScanAndWait(timeout=120)
    scanStarted()                              # already emitted: returns at once
    print(f'Scan started and done in {time.monotonic() - t0:.1f} s')
finally:
    api.imcontrol.loadScanParamsFromFile(backup)
    mainWindow.setCurrentModule('imscripting')

# The camera took one picture per pulse, but nothing kept them: the live
# view only shows the latest. The next tutorial records them.

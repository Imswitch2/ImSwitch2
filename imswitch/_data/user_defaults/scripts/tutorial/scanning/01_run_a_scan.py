"""Tutorial scanning 01 -- Run a scan that triggers the camera.

You will learn
  * how a scan drives a camera: the stages step through the positions and
    the camera gets one trigger pulse (TTL) per position
  * ``loadScanParamsFromFile()`` / ``saveScanParamsToFile()``: scan settings
    as files -- design them in the Scan widget, save, load in a script
  * ``runScanAndWait()``: run one scan and wait for exactly that scan
  * ``getWaitForSignal()``: find out whether something happened
  * to set the camera's trigger mode for a scan, and put it back

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) that takes
                a picture only when it receives a trigger pulse, X/Y/Z
                stages, and an NI-DAQ card that runs the scan and sends the
                pulses.
  Your own microscope: needs a Scan widget of the Base type with X/Y
                stages, and a camera the scan can trigger (one with a
                digitalLine in the setup file).

  scan_params/camera_scan_1um.json in this folder holds the scan settings;
  the names of your camera and stages are set at the top of the script.

Next: 02_record_a_scan.py
"""

import json
import os
import tempfile
import time

# The names of this setup's devices. On another setup, change them here --
# they are put into the scan settings below, so the settings file itself
# can stay as it is.
CAMERA = 'Camera'                      # the camera the scan triggers
STAGES = ['X', 'Y', 'Z']               # the scan axes, fastest first

# The camera must take a picture when -- and only when -- a pulse arrives,
# with an exposure shorter than the 10 ms between pulses. The simulated
# Hamamatsu starts that way, but a script or you may have changed it.
# (Names as in the Settings widget; getDetectorParameters(CAMERA) lists yours.)
CAMERA_SETTINGS = {'Trigger source': 'External "frame-trigger"',
                   'Set exposure time': 0.005}                  # s

# getScriptDirPath() is this script's folder, so the settings file is found
# wherever the tutorials were copied to.
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'camera_scan_1um.json')

# A setup can have several scan widgets; getScanSourceNames() lists them and
# runScanAndWait(source=...) picks one. This setup has one, called 'Scan'.
print('Scan sources:', api.imcontrol.getScanSourceNames())

# A scan settings file holds what the Scan widget shows, as JSON -- you
# can open it from the Files panel. This one moves the stages over 1 µm in
# 0.1 µm steps -- 10 x 10 positions, 10 ms at each -- and sends the camera
# a pulse from 0 to 5 ms of every step. Read it, and put this setup's
# device names into it: the stages in two places, the camera in one.
with open(PARAMS, encoding='utf-8') as file:
    settings = json.load(file)
analog, digital = settings['analogParameterDict'], settings['digitalParameterDict']
analog['target_device'] = STAGES
settings['positionersScan'] = STAGES
digital['target_device'] = [CAMERA]
print('Axes:', analog['target_device'], 'lengths (µm):', analog['axis_length'],
      'steps (µm):', analog['axis_step_size'])
print('Triggered:', digital['target_device'], 'from', digital['TTL_start'],
      'to', digital['TTL_end'], 's of each', digital['sequence_time'], 's step')

# loadScanParamsFromFile() takes a file, so write the edited settings to a
# temporary one. (tempfile.gettempdir() is the system's folder for those.)
loaded = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan.json')
with open(loaded, 'w', encoding='utf-8') as file:
    json.dump(settings, file, indent=2)

# Loading the file changes the Scan widget. Save what it shows now, so it
# can be put back at the end -- and remember the camera's settings too.
backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
previousCamera = {name: api.imcontrol.getDetectorParameter(CAMERA, name)
                  for name in CAMERA_SETTINGS}

signals = api.imcontrol.signals()

try:
    for name, value in CAMERA_SETTINGS.items():
        api.imcontrol.setDetectorParameter(CAMERA, name, value)
    api.imcontrol.loadScanParamsFromFile(loaded)
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
    for name, value in previousCamera.items():
        api.imcontrol.setDetectorParameter(CAMERA, name, value)
    mainWindow.setCurrentModule('imscripting')

# The camera took one picture per pulse, but nothing kept them: the live
# view only shows the latest. The next tutorial records them.

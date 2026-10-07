"""Tutorial basic 01 -- Hello, ImSwitch: what a script can reach.

You will learn
  * that a script talks to the microscope through ``api.imcontrol``
  * how to find out which devices the loaded setup has
  * the two ways to write to the Output panel: ``print()`` and ``getLogger()``
  * why a script pauses with ``sleep()`` and never with ``time.sleep()``

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera") and an XY stage. Nothing is connected;
                the camera returns noise images.
  Your own microscope: any setup works -- this script only looks.

How to run it
  Read the script first and guess what it will print. Then press Run all
  (above the editor) and compare your guess with the Output panel below the
  editor. Stop ends a script at any time. tutorial/README.md explains how
  to load the setup named above.

Next: 02_snap_an_image.py
"""

# A script runs *inside* ImSwitch, so some names exist without an import.
# The most important ones:
#
#   api.imcontrol   the microscope: cameras, stages, lasers, recording, scans
#   mainWindow      the ImSwitch window, e.g. mainWindow.setCurrentModule(...)
#   getLogger(), sleep(), waitUntil(), callAndWaitForSignal(),
#   getWaitForSignal(), runScanAndWait(), importScript(), getScriptDirPath()
#
# A code editor outside ImSwitch will mark them as undefined; inside
# ImSwitch they exist. Everything else -- numpy, h5py, your own code -- is
# imported as in any Python file.

# print() writes plain text to the Output panel. Use it for results: the
# things you want to read afterwards.
print('Cameras and other detectors:', api.imcontrol.getDetectorNames())
print('Recordings are saved in:', api.imcontrol.getRecFolder())

# api.imcontrol only offers the functions that the setup's panels (widgets)
# provide. example_mock.json simulates a stage but has no Positioner panel,
# so there is no getPositionerNames() here. Calling it raises an
# AttributeError that says why; "try ... except" catches that error so the
# script can carry on instead of stopping.
try:
    print('Stages:', api.imcontrol.getPositionerNames())
except AttributeError:
    print('No Positioner widget in this setup, so no stage functions.')

# getLogger() gives you a *logger*. It also writes to the Output panel, but
# it puts the time and a level in front of every line, the way ImSwitch
# reports its own messages:
#   log.info(...)     progress: what the script is doing now
#   log.warning(...)  something worth a look, but the script goes on
#   log.error(...)    something went wrong
# A rule of thumb: print() for the results, the logger for "what happened
# when" -- the timestamps show how long each step took.
log = getLogger()
log.info('Counting down...')

# sleep(seconds) pauses the script. Always use it rather than time.sleep():
# sleep() returns at once when you press Stop, while time.sleep() would keep
# the script -- and anything it switched on -- running until the time is up.
for second in (3, 2, 1):
    log.info(f'{second}...')   # f'...' puts the value of `second` into the text
    sleep(1)

log.info('Hello from your first ImSwitch script.')

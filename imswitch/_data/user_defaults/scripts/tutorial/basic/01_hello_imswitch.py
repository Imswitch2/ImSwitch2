"""Tutorial basic 01 -- Hello, ImSwitch: what a script can reach.

You will learn
  * that a script talks to the microscope through ``api.imcontrol``
  * how to find out which devices the loaded setup has
  * how to write to the Output panel with ``print`` and ``getLogger()``
  * why a script pauses with ``sleep()`` and never with ``time.sleep()``

Setup
  Mock setup: example_mock.json
  Needs:      any setup -- this script only looks.

  Pick the setup in the Hardware Control tab with Tools > Pick hardware
  setup... (ImSwitch restarts), then open this file in the Scripting tab and
  press Run. Stop ends a script at any time.

Next: 02_snap_an_image.py
"""

# A script runs inside ImSwitch, so a few names exist without an import:
#
#   api.imcontrol   the microscope: cameras, stages, lasers, recording, scans
#   mainWindow      the window, e.g. mainWindow.setCurrentModule('imcontrol')
#   getLogger(), sleep(), waitUntil(), callAndWaitForSignal(),
#   getWaitForSignal(), runScanAndWait(), importScript(), getScriptDirPath()
#
# Your editor will call them undefined; that is expected. Everything else --
# numpy, h5py, your own code -- is imported as in any Python file.

log = getLogger()

print('Cameras and other detectors:', api.imcontrol.getDetectorNames())
print('Recordings are saved in:', api.imcontrol.getRecFolder())

# api.imcontrol only has what the setup's widgets provide. example_mock.json
# has a stage but no Positioner widget, so there is no getPositionerNames()
# here -- asking for it raises an AttributeError that says why.
try:
    print('Stages:', api.imcontrol.getPositionerNames())
except AttributeError:
    print('No Positioner widget in this setup, so no stage functions.')

# sleep() returns at once when you press Stop. time.sleep() would keep the
# script -- and whatever it has switched on -- running until the time is up.
for second in (3, 2, 1):
    log.info(f'{second}...')
    sleep(1)

log.info('Hello from your first ImSwitch script.')

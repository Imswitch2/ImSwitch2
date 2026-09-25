"""Tutorial basic 08 -- Share code between scripts with importScript().

You will learn
  * ``importScript()``: load functions from another file next to this one
  * ``getScriptDirPath()``: the folder of the running script

Setup
  Mock setup: example_mock.json
  Needs:      a camera and the Recording widget.

  Uses tutorial_helpers.py from this folder -- open it to see the functions.

Next: the scanning tutorials in ../scanning, starting with 01_run_a_scan.py
"""

# The path is relative to this script. A plain "import tutorial_helpers"
# would not work: the script folder is not on Python's search path, and the
# helper would not get api and the other script names.
helpers = importScript('tutorial_helpers.py')
print('Loaded helpers from', getScriptDirPath())

camera = api.imcontrol.getDetectorNames()[0]
for numFrames in (3, 6):
    name = f'tutorial_{numFrames}_frames'
    helpers.recordFrames(numFrames, name)
    print(f'{numFrames} frames ->', helpers.newestRecording(name, camera))

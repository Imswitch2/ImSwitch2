"""Tutorial basic 06 -- Share code between scripts with importScript().

You will learn
  * ``importScript()``: load functions from another file next to this one
  * ``getScriptDirPath()``: the folder of the running script

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera", 800 x 800 pixels) that streams noise
                images.
  Your own microscope: needs a camera and the Recording widget.

  Uses tutorial_helpers.py from this folder -- open it to see the functions.

Next: 07_two_cameras.py (on a different mock setup)
"""

# When several scripts need the same steps, write them once as functions
# in a separate file and load that file with importScript(). The path is
# relative to this script's folder.
#
# A plain "import tutorial_helpers" would not work: the script folder is
# not where Python looks for modules, and the helper file would not get
# api and the other names that ImSwitch gives a script.
helpers = importScript('tutorial_helpers.py')
print('Loaded helpers from', getScriptDirPath())

camera = api.imcontrol.getDetectorNames()[0]
# helpers.applyCameraSettings() sets the camera settings a recording relies
# on (see tutorial 04) and returns the previous ones, to put back.
previousSettings = helpers.applyCameraSettings(camera, {'exposure': 20})   # ms
try:
    for numFrames in (3, 6):
        name = f'tutorial_{numFrames}_frames'
        # helpers.recordFrames() is tutorial 04 in one line: set the mode, the
        # name and the format, record, wait, and put those settings back.
        helpers.recordFrames(numFrames, name)
        print(f'{numFrames} frames ->', helpers.newestRecording(name, camera))
finally:
    helpers.applyCameraSettings(camera, previousSettings)

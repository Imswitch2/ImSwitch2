"""Tutorial basic 09 -- Inputs you can set without editing the script.

You will learn
  * ``scriptInput()``: declare a value that can be set from outside
  * the Script Runner widget: choose this file there and it shows a field
    for every input, plus Run and Stop buttons

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera") that returns noise images.
  Your own microscope: needs a camera. For the fields, add ``ScriptRunner``
                to ``availableWidgets`` (Config editor, Widgets, Advanced)
                and enable the Scripting module.

Next: the scanning tutorials, starting with ../scanning/01_run_a_scan.py
"""

# scriptInput(name, default, description) returns the value typed into the
# Script Runner field called name. Run from this editor, or with the field
# left empty, it returns the default. The default also decides what may be
# typed: 10.0 is a number, 3 a whole number, True a yes/no, 'x' any text.
#
# The widget finds these lines without running the script, so write the
# name and the default as plain values, not as variables or calculations.
exposureMs = scriptInput('exposure_ms', 10.0, 'Exposure time per image, in ms')
numImages = scriptInput('images', 3, 'How many images to snap')
note = scriptInput('note', 'test run', 'Printed with the results')

camera = api.imcontrol.getDetectorNames()[0]
EXPOSURE = 'exposure'     # the simulated camera's name for it, see tutorial 03

previousExposure = api.imcontrol.getDetectorParameter(camera, EXPOSURE)
try:
    api.imcontrol.setDetectorParameter(camera, EXPOSURE, exposureMs)
    print(f'{note}: {numImages} image(s) at {exposureMs} ms')
    for index in range(numImages):
        image = api.imcontrol.snapImage(True)[camera]
        print(f'  image {index + 1}: mean {image.mean():.1f}')
finally:
    api.imcontrol.setDetectorParameter(camera, EXPOSURE, previousExposure)

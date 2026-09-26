"""Tutorial basic 03 -- Camera settings and the live view.

You will learn
  * ``getDetectorParameters()``: which settings a camera has, and their
    units -- the names differ from camera to camera
  * ``getDetectorParameter()`` and ``setDetectorParameter()``: read and
    change exposure, gain -- any value in the Settings widget
  * ``setDetectorROI()``: read out only part of the sensor
  * ``setLiveViewActive()``: start and stop the live view from a script
  * ``try``/``finally``: leave the camera as you found it, even on Stop

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera", 800 x 800 pixels) with an exposure
                time, a gain and a region of interest you can set.
  Your own microscope: needs a camera, and the Settings, View and
                Recording widgets.

Next: 04_record_frames.py
"""

camera = api.imcontrol.getDetectorNames()[0]

# Parameter names and units belong to the camera: they are the ones listed
# in the Settings widget, and getDetectorParameters() lists them for a
# script -- a dictionary {name: details}. The details are the value, the
# units, whether a script may change it ('editable'), and for a setting
# picked from a list, the choices ('options').
for name, details in api.imcontrol.getDetectorParameters(camera).items():
    units = details['units'] or ''      # None for a choice from a list
    readOnly = '' if details['editable'] else '   (read only)'
    print(f'{name:>25}: {details["value"]} {units}{readOnly}')

# This simulated camera calls its exposure 'exposure' and counts in ms; a
# Thorlabs camera says 'Exposure' in µs, a Hamamatsu 'Set exposure time' in
# s. On your own microscope, use the name the list above prints.
EXPOSURE = 'exposure'

# Read the exposure before changing it, to put your value back at the end.
previousExposure = api.imcontrol.getDetectorParameter(camera, EXPOSURE)

# Remember the full sensor size so the ROI can be undone at the end.
FULL_SENSOR = api.imcontrol.snapImage(True)[camera].shape   # (rows, columns)

# Everything in "try" may change the camera. The "finally" block below runs
# afterwards in every case -- when the try block finishes, when it fails,
# and when you press Stop -- so the camera is always set back.
try:
    for exposure in (10, 50, 200):
        api.imcontrol.setDetectorParameter(camera, EXPOSURE, exposure)
        image = api.imcontrol.snapImage(True)[camera]
        # A real camera's image gets brighter with a longer exposure; the
        # simulated camera's noise does not care, so expect similar means.
        print(f'exposure {exposure:>3} ms -> mean {image.mean():.1f}')

    # A region of interest (ROI): only part of the sensor is read out, which
    # is faster and gives smaller files. The arguments are the top-left
    # corner (x, y) and the size (width, height), in pixels.
    api.imcontrol.setDetectorROI(camera, (200, 100), (400, 300))
    image = api.imcontrol.snapImage(True)[camera]
    print(f'with the ROI the image is {image.shape[1]} x {image.shape[0]} pixels')

    # Show the live view for a few seconds. mainWindow.setCurrentModule()
    # switches to the Hardware Control tab so you can watch it.
    mainWindow.setCurrentModule('imcontrol')
    api.imcontrol.setLiveViewActive(True)
    sleep(3)
finally:
    # Undo everything, in reverse order.
    api.imcontrol.setLiveViewActive(False)
    api.imcontrol.setDetectorROI(camera, (0, 0), (FULL_SENSOR[1], FULL_SENSOR[0]))
    api.imcontrol.setDetectorParameter(camera, EXPOSURE, previousExposure)
    mainWindow.setCurrentModule('imscripting')     # back to the Scripting tab

print('restored: full sensor', api.imcontrol.snapImage(True)[camera].shape,
      'and exposure', api.imcontrol.getDetectorParameter(camera, EXPOSURE))

"""Tutorial basic 03 -- Camera settings and the live view.

You will learn
  * ``setDetectorParameter()``: exposure, gain -- any value in the Settings
    widget -- and why the names differ from camera to camera
  * ``setDetectorROI()``: read out only part of the sensor
  * ``setLiveViewActive()``: start and stop the live view from a script
  * to leave the camera as you found it, even when you press Stop

Setup
  Mock setup: example_mock.json
  Needs:      a camera, and the Settings, View and Recording widgets.

Next: 04_record_frames.py
"""

camera = api.imcontrol.getDetectorNames()[0]

# Parameter names and units are the camera's own: the ones in the Settings
# widget. The mock camera of example_mock.json calls its exposure 'exposure'
# and counts in ms; a Thorlabs camera says 'Exposure' in µs, a Hamamatsu
# 'Set exposure time' in s. Check the Settings widget for yours.
EXPOSURE, EXPOSURE_DEFAULT = 'exposure', 100      # ms, this mock's start value
FULL_SENSOR = api.imcontrol.snapImage(True)[camera].shape   # (rows, columns)

try:
    for exposure in (10, 50, 200):
        api.imcontrol.setDetectorParameter(camera, EXPOSURE, exposure)
        image = api.imcontrol.snapImage(True)[camera]
        # A real camera's image gets brighter; the mock's noise does not care.
        print(f'exposure {exposure:>3} ms -> mean {image.mean():.1f}')

    # A region of interest: start (x, y) and size (width, height) in pixels.
    # A smaller readout is faster and makes smaller files.
    api.imcontrol.setDetectorROI(camera, (200, 100), (400, 300))
    image = api.imcontrol.snapImage(True)[camera]
    print(f'with the ROI the image is {image.shape[1]} x {image.shape[0]} pixels')

    # Show the live view for a few seconds. mainWindow switches the visible
    # tab so you can watch it; sleep() keeps the script responsive to Stop.
    mainWindow.setCurrentModule('imcontrol')
    api.imcontrol.setLiveViewActive(True)
    sleep(3)
finally:
    # finally runs even when you press Stop, so the camera always ends up as
    # it started. There is no api function that reads a detector parameter,
    # so the values to restore are written down above.
    api.imcontrol.setLiveViewActive(False)
    api.imcontrol.setDetectorROI(camera, (0, 0), (FULL_SENSOR[1], FULL_SENSOR[0]))
    api.imcontrol.setDetectorParameter(camera, EXPOSURE, EXPOSURE_DEFAULT)
    mainWindow.setCurrentModule('imscripting')

print('restored: full sensor', api.imcontrol.snapImage(True)[camera].shape)

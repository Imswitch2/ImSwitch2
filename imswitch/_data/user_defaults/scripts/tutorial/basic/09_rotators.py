"""Tutorial basic 09 -- Turn a rotation mount and take an image per angle.

You will learn
  * the rotator API: ``getRotatorNames()``, ``getRotatorPosition()``,
    ``setRotatorPosition()`` (absolute) and ``moveRotator()`` (relative)
  * a polarization series: one image per half-wave-plate angle
  * to put the mount back where it was, whatever happens

Setup
  Mock setup:   example_no_hardware.json
  It simulates: two cameras, three stages and three rotation mounts --
                "Mock HWP" and "Mock QWP" (Elliptec) and "Mock K10CR1"
                (Kinesis). The mounts arrive at once and report their angle.
  Your own microscope: needs a rotation mount, a camera, and the Rotator
                and Recording widgets. Change MOUNT to your mount's name
                (getRotatorNames() prints them). The mock camera's images
                do not change with the angle; yours will.

Next: 10_frames_into_memory.py
"""

MOUNT = 'Mock HWP'                       # a name from getRotatorNames()
ANGLES_DEG = [0, 22.5, 45, 67.5, 90]     # half-wave plate: 0-90° covers it

print('Rotators:', api.imcontrol.getRotatorNames())
camera = api.imcontrol.getDetectorNames()[0]
api.imcontrol.setDetectorToRecord(camera)         # snapImage() snaps this one

# Rotator angles are in degrees. Remember where the mount is so the script
# can leave it there.
start = api.imcontrol.getRotatorPosition(MOUNT)
print(f'{MOUNT} is at {start:.1f}°')

intensity = {}
try:
    for angle in ANGLES_DEG:
        # setRotatorPosition() turns to an absolute angle and returns when
        # the mount has arrived, so the image is taken at that angle.
        api.imcontrol.setRotatorPosition(MOUNT, angle)
        arrived = api.imcontrol.getRotatorPosition(MOUNT)
        assert abs(arrived - angle) < 0.5, f'{MOUNT} is at {arrived}°, not {angle}°'
        image = api.imcontrol.snapImage(True)[camera]
        intensity[angle] = float(image.mean())
        print(f'  {angle:5.1f}°: mean intensity {intensity[angle]:.1f}')

    # moveRotator() turns by an angle from where the mount is, either way.
    api.imcontrol.moveRotator(MOUNT, 10.0)
    api.imcontrol.moveRotator(MOUNT, -10.0)
    print(f'{MOUNT} after +10° and -10°: {api.imcontrol.getRotatorPosition(MOUNT):.1f}°')
finally:
    # Back to the starting angle, also when a snap failed or Stop was pressed.
    api.imcontrol.setRotatorPosition(MOUNT, start)
    api.imcontrol.setDetectorToRecord(-1)         # the camera shown, as before

brightest = max(intensity, key=intensity.get)
print(f'brightest at {brightest}° (on a real microscope: the polarization that '
      'passes the analyser)')

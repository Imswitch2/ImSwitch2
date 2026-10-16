"""Tutorial scanning 08 -- What each frame was: structured results and temporary settings.

You will learn
  * ``recordScanToMemory(..., structured=True)``: the frames together with
    what they mean -- axes, pixel size, which scan position each frame was
    taken at, the settings the recording wrote down, whether it is complete
  * where those positions come from: recorded by the scan, or reconstructed
    from its plan
  * ``temporaryDetectorSettings()`` and ``temporaryScanParameters()``:
    settings for one block, put back afterwards whatever happens
  * ``result.save()``: keep a result after all, as the recording's own file

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) triggered by
                the scan, X/Y/Z stages, and an NI-DAQ card that runs the
                scan.
  Your own microscope: as 01_run_a_scan.py, plus the Recording widget.

Next: that was the last tutorial. The workflows folder has complete
      acquisitions for real microscopes.
"""
import os
import tempfile

CAMERA = 'Camera'
STAGES = ['X', 'Y', 'Z']

scan = importScript('scan_helpers.py')

# Both managers remember what they change and put it back when the block
# ends -- normally, on an error, or when Stop is pressed. They never move a
# stage; returningToPositions() is the one that does, when you ask for it.
with temporaryScanParameters(), \
        temporaryDetectorSettings(CAMERA, {'Trigger source': 'External "frame-trigger"',
                                           'Set exposure time': 0.005}):
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)
    params = api.imcontrol.getScanParameters()
    params['analogParameterDict']['axis_step_size'][0] = 0.2    # X: 5 steps of 0.2 µm
    api.imcontrol.setScanParameters(params)
    result = api.imcontrol.recordScanToMemory(CAMERA, timeout=120, structured=True)

print(result)
camera = result.detector(CAMERA)
print(f'axes {camera.axes}, pixel size {camera.pixelSizeUm} µm, '
      f'{camera.framesRecorded} of {camera.framesExpected} frames, {camera.outcome}')
assert result.complete and camera.framesRecorded == 50          # 5 x 10 positions

# Which scan position each frame was taken at. This scan (the Base one)
# does not record its layout, so the positions are reconstructed from the
# plan the file records -- positions.source says 'plan'; the Advanced and
# point scans record theirs ('recorded'). Either way they are the commanded
# positions: the stages are not read back frame by frame.
positions = camera.positions
offsets = positions.offsets()
print(f'positions from the {positions.source}; loops {positions.loops}, '
      f'moved by {positions.devices}')
for frame in (0, 1, 5):
    where = ', '.join(f'{device} +{offset:.2f}' for device, offset
                      in zip(positions.devices, offsets[frame]))
    print(f'  frame {frame}: {where} µm from the first position')
assert positions.indices[1].tolist() == [0, 0, 1]               # X moves fastest

# What the recording wrote down -- the settings in force, not the widgets'
# current ones (the camera is back on its own trigger by now).
exposure = camera.settings['Detector'][f'{CAMERA}:Param:Set exposure time']
print(f'recorded with an exposure of {exposure} s, started {result.startedAt:%H:%M:%S}')

# Keep it after all: the recording's own HDF5 file, every attribute included.
paths = result.save(tempfile.mkdtemp())
print('saved', os.path.basename(paths[0]))
result.show('structured')

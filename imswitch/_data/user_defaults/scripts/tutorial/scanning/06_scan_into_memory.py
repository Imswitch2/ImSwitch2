"""Tutorial scanning 06 -- A scan's frames as an array, without a file.

You will learn
  * ``recordScanToMemory()``: run the scan the Scan widget holds once and
    get every frame it triggers back as a NumPy array -- nothing written
  * that it blocks until the scan is over, and puts the Recording widget
    back as it was
  * to work on the frames in the script (here: the mean image)

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) triggered by
                the scan, X/Y/Z stages, and an NI-DAQ card that runs the
                scan.
  Your own microscope: as 01_run_a_scan.py, plus the Recording widget.

Next: 07_scan_parameters_in_code.py
"""
import glob
import os

# The names of this setup's devices -- change them for yours (see 01).
CAMERA = 'Camera'
STAGES = ['X', 'Y', 'Z']

# Trigger mode and exposure the scan needs from the camera (see 01).
CAMERA_SETTINGS = {'Trigger source': 'External "frame-trigger"',
                   'Set exposure time': 0.005}                  # s

# The steps of tutorial 01 as functions (see 02).
scan = importScript('scan_helpers.py')

# Nothing is written: count the files in the recordings folder before and
# after to see that for yourself.
folder = api.imcontrol.getRecFolder()
filesBefore = set(glob.glob(os.path.join(folder, '*')))

backup = scan.backupScanSettings()
previousCamera = scan.applyCameraSettings(CAMERA, CAMERA_SETTINGS)
try:
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)
    # One call: it starts the scan, waits for its end and returns the frames
    # the scan triggered, one per position, as (frames, rows, columns).
    frames = api.imcontrol.recordScanToMemory(CAMERA, timeout=120)
finally:
    api.imcontrol.loadScanParamsFromFile(backup)
    scan.applyCameraSettings(CAMERA, previousCamera)

print(f'{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px, '
      f'{frames.dtype}, {frames.nbytes / 1e6:.0f} MB in memory')
assert frames.shape[0] == 100, 'expected one frame per scan position'
assert set(glob.glob(os.path.join(folder, '*'))) == filesBefore, 'a file was written'

# The frames are yours: here the mean over the scan, which for a MoNaLISA
# or ISM lattice is the pattern itself.
mean = frames.mean(axis=0)
print(f'mean frame: {mean.min():.1f} to {mean.max():.1f} counts')

# Several detectors give a dict: api.imcontrol.recordScanToMemory(['Camera',
# 'APD'])['APD']. keep=True also lists the recording in ImProcess.

"""Tutorial scanning 07 -- Scan settings as a dict, changed in the script.

You will learn
  * ``getScanParameters()`` / ``setScanParameters()``: the Scan widget's
    settings as a dict, to read and change in the script instead of a file
  * what the dict holds: the scanned axes with their lengths, steps and
    centres, and the trigger pulses
  * to check the result with ``recordScanToMemory()``: one frame per
    scan position

Setup
  Mock setup:   hamamatsu_mock_scan_setup.json
  It simulates: a Hamamatsu camera ("Camera", 512 x 512 pixels) triggered by
                the scan, X/Y/Z stages, and an NI-DAQ card that runs the
                scan.
  Your own microscope: as 01_run_a_scan.py, plus the Recording widget.

Next: that was the last tutorial. The workflows folder has complete
      acquisitions for real microscopes.
"""
import numpy as np

# The names of this setup's devices -- change them for yours (see 01).
CAMERA = 'Camera'
STAGES = ['X', 'Y', 'Z']
CAMERA_SETTINGS = {'Trigger source': 'External "frame-trigger"',
                   'Set exposure time': 0.005}                  # s

scan = importScript('scan_helpers.py')
backup = scan.backupScanSettings()
previousCamera = scan.applyCameraSettings(CAMERA, CAMERA_SETTINGS)
try:
    scan.loadScanSettings('camera_scan_1um.json', CAMERA, STAGES)

    # The Scan widget's settings, as a dict -- the same content that
    # saveScanParamsToFile() writes. 'analogParameterDict' is the scan:
    # one entry per axis, in µm, fastest axis first.
    params = api.imcontrol.getScanParameters()
    analog = params['analogParameterDict']
    print('axes:   ', analog['target_device'])
    print('lengths:', analog['axis_length'], 'µm')
    print('steps:  ', analog['axis_step_size'], 'µm')
    print('centres:', analog['axis_centerpos'], 'µm')
    print('pulses: ', params['digitalParameterDict']['target_device'],
          'from', params['digitalParameterDict']['TTL_start'], 's')

    # Change it in the script: half the length along the first axis, at the
    # same step, is half the positions -- and give it back to the widget.
    analog['axis_length'][0] = analog['axis_length'][0] / 2
    api.imcontrol.setScanParameters(params)

    # One frame per scan position: the frame count checks the geometry.
    frames = api.imcontrol.recordScanToMemory(CAMERA, timeout=120)
    positions = [round(length / step) for length, step
                 in zip(analog['axis_length'], analog['axis_step_size'])]
    print(f'{frames.shape[0]} frames for {" x ".join(map(str, positions))} positions')
    assert frames.shape[0] == int(np.prod(positions))
finally:
    api.imcontrol.loadScanParamsFromFile(backup)
    scan.applyCameraSettings(CAMERA, previousCamera)

# A dict that does not fit the setup -- a device it lacks, say -- makes
# setScanParameters() raise ValueError naming what could not be applied,
# instead of leaving the widget half changed.

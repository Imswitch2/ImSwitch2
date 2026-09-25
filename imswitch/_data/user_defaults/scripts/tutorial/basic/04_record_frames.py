"""Tutorial basic 04 -- Record a number of frames and read them back.

You will learn
  * the recording modes: ``setRecModeSpecFrames()``, ``setRecModeSpecTime()``
  * ``setRecFilename()`` and ``setRecFileFormat()``: name and format
  * ``callAndWaitForSignal()``: start something and wait until it is done
  * how to open the recording with h5py

Setup
  Mock setup: example_mock.json
  Needs:      a camera and the Recording widget.

Next: 05_two_cameras.py
"""

import glob
import os

import h5py

camera = api.imcontrol.getDetectorNames()[0]
signals = api.imcontrol.signals()
NAME = 'tutorial_20_frames'

# The format is the Recording widget's "File format". This script reads the
# file with h5py, so it asks for HDF5 -- and remembers yours to put it back.
previousFormat = api.imcontrol.getRecFileFormat()
api.imcontrol.setRecFileFormat('HDF5')
api.imcontrol.setRecModeSpecFrames(20)     # or setRecModeSpecTime(seconds)
api.imcontrol.setRecFilename(NAME)
try:
    # startRecording() returns at once; the recording then runs on its own.
    # callAndWaitForSignal() listens for recordingEnded *before* it calls
    # startRecording, so even a very short recording cannot slip past it.
    # (Creating the waiter afterwards could miss the signal and wait forever.)
    # timeout: give up with a TimeoutError rather than hang if it never ends.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=60)
finally:
    api.imcontrol.setRecFilename(None)     # back to time-stamped names
    api.imcontrol.setRecFileFormat(previousFormat)

# Files are named <name>_rec_<detector>.<format>. An existing file is never
# overwritten: a second run adds _1, _2, ... -- so take the newest.
folder = api.imcontrol.getRecFolder()
path = max(glob.glob(os.path.join(folder, f'{NAME}_rec_{camera}*.hdf5')),
           key=os.path.getmtime)
print('Recorded', path)

with h5py.File(path, 'r') as f:
    frames = f[f'{camera}/data']              # (frames, rows, columns)
    print(f'{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px')
    print(f'mean of the first frame: {frames[0].mean():.1f}, '
          f'of the last: {frames[-1].mean():.1f}')

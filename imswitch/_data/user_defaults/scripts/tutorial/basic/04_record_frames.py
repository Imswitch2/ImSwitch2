"""Tutorial basic 04 -- Record a number of frames and read them back.

You will learn
  * the recording modes: ``setRecModeSpecFrames()``, ``setRecModeSpecTime()``
  * ``setRecFilename()`` and ``setRecFileFormat()``: name and format
  * ``callAndWaitForSignal()``: start something and wait until it is done
  * how to open the recording with h5py

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera", 800 x 800 pixels) that streams noise
                images, so a recording has real frames to save.
  Your own microscope: needs a camera and the Recording widget.

Next: 05_record_until_stop_safely.py
"""

import glob
import os

import h5py

camera = api.imcontrol.getDetectorNames()[0]
# signals() gives the events ImSwitch announces -- "recording started",
# "recording ended", "scan ended", ... -- so a script can wait for them.
signals = api.imcontrol.signals()
NAME = 'tutorial_20_frames'

# The file format is the Recording widget's "File format". This script
# reads the file with h5py, which needs HDF5, so it asks for HDF5 -- and
# first remembers your choice, to put it back afterwards.
previousFormat = api.imcontrol.getRecFileFormat()
api.imcontrol.setRecFileFormat('HDF5')

# The recording mode says when a recording stops by itself: after a number
# of frames here; setRecModeSpecTime(seconds) would stop after a time.
api.imcontrol.setRecModeSpecFrames(20)
api.imcontrol.setRecFilename(NAME)
try:
    # startRecording() returns at once; the recording then runs on its own.
    # callAndWaitForSignal(signal, function) first starts listening for the
    # signal, then calls the function, then waits for the signal. Listening
    # *before* starting matters: a short recording can be over before a
    # waiter created afterwards exists, and that waiter would wait forever.
    # timeout: give up with a TimeoutError rather than hang if it never ends.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=60)
finally:
    api.imcontrol.setRecFilename(None)     # back to time-stamped names
    api.imcontrol.setRecFileFormat(previousFormat)

# Files are named <name>_rec_<camera>.<format>. An existing file is never
# overwritten: running the script again gives ..._1, ..._2 -- so look for
# every matching file (glob) and take the newest (largest modification time).
folder = api.imcontrol.getRecFolder()
path = max(glob.glob(os.path.join(folder, f'{NAME}_rec_{camera}*.hdf5')),
           key=os.path.getmtime)
print('Recorded', path)

# In the HDF5 file each camera has a group, and the frames are its 'data':
# an array shaped (frames, rows, columns). "with" closes the file afterwards.
with h5py.File(path, 'r') as f:
    frames = f[f'{camera}/data']
    print(f'{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px')
    print(f'mean of the first frame: {frames[0].mean():.1f}, '
          f'of the last: {frames[-1].mean():.1f}')

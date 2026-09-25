"""Helpers for tutorial basic 06 -- not a script to run on its own.

Imported by: 06_share_code_between_scripts.py

A file of functions that several scripts can share. importScript() gives
this module the same names a script has (``api``, ``sleep``,
``callAndWaitForSignal``, ...), but only after the file has been loaded:
use them inside functions, as below, not at the top level of the file.
"""

import glob
import os


def newestRecording(name, detector):
    """Path of the newest recording called ``name`` for ``detector``.

    A second recording with the same name gets _1, _2, ... appended, so
    this finds every match and returns the most recently written one.
    """
    pattern = os.path.join(api.imcontrol.getRecFolder(), f'{name}_rec_{detector}*')
    return max(glob.glob(pattern), key=os.path.getmtime)


def recordFrames(numFrames, name, fileFormat='HDF5', timeout=60):
    """Record ``numFrames`` frames from the selected detectors into files
    called ``name``, wait until they are written, and put the file name and
    format settings back afterwards -- tutorial 04 as one function."""
    previousFormat = api.imcontrol.getRecFileFormat()
    api.imcontrol.setRecFileFormat(fileFormat)
    api.imcontrol.setRecModeSpecFrames(numFrames)
    api.imcontrol.setRecFilename(name)
    try:
        callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
                             api.imcontrol.startRecording, timeout=timeout)
    finally:
        api.imcontrol.setRecFilename(None)
        api.imcontrol.setRecFileFormat(previousFormat)

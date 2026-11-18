"""Polarization stacks -- one recorded stack per pair of waveplate angles.

For the polarization rig that ran Microscope_GUI (its polarization_scan
branch): a Nucleo step board times the two Cobolts (405, 488), the camera
and the two galvos; two Elliptec mounts each hold a waveplate. For every
angle pair in ANGLE_PAIRS_DEG the script turns both mounts, then records
one of the rig's scans -- Wide Field 2D or Line Scan 2D -- as one stack.

Each stack says which angles it was taken at: in its file name, and in the
metadata recorded with its frames -- the Rotator group, ``WaveplateA:Position``
and ``WaveplateB:Position``, the angles the mounts reported (in an HDF5 file:
``Hamamatsu/metadata/Rotator``).

Before the first run
  * Write the rig's scans, once and again whenever the galvos' conversion
    factors or the old GUI's values change:
        python tools/polarization_rig_scans.py --setup <the rig's setup>
    They go to <ImSwitchConfig>/imcontrol_scans/polarization_rig, where
    this script reads them (SCAN_FOLDER).
  * Set the lasers' powers in the Laser widget; the scan only gates them.
  * The camera in external trigger mode: the scan's pulses take the frames.

Mock setup: nucleo_polarization_mock_setup.json -- a simulated Nucleo, mock
Cobolts, a mock Hamamatsu, the BPC303 piezo and the Elliptec waveplates.
"""
# ruff: noqa: F821
import contextlib
import json
import os

from imswitch.imcommon.model import dirtools

# The setup's names -- change them for yours.
MOUNTS = ('WaveplateA', 'WaveplateB')
CAMERA = 'Hamamatsu'

# One stack per pair, (WaveplateA, WaveplateB) in degrees. Placeholders: the
# rig's angles are not decided yet.
ANGLE_PAIRS_DEG = [(0.0, 0.0), (22.5, 0.0), (45.0, 0.0), (67.5, 0.0)]

# 'wide_field_2d' or 'line_scan_2d' (a file the tool wrote, without .json).
SCAN = 'wide_field_2d'
# The old Line Scan 2D recorded dark frames (camera only) first: a stack of
# its own, taken once before the series.
DARK_FRAMES_FIRST = SCAN == 'line_scan_2d'

NAME = 'polarization'                     # the recordings' file names start with it
ANGLE_TOLERANCE_DEG = 0.5                 # a mount further off stops the series
STACK_TIMEOUT_S = 600                     # per stack

SCAN_FOLDER = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans', 'polarization_rig')
signals = api.imcontrol.signals()


def readScan(name):
    path = os.path.join(SCAN_FOLDER, f'{name}.json')
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f'{path} is missing: write the rig\'s scans first with '
            'python tools/polarization_rig_scans.py --setup <the rig\'s setup>')
    with open(path, encoding='utf-8') as file:
        return json.load(file)


def recordStack(scan, fileName):
    """Record one run of ``scan`` (a scan file's content) as one stack."""
    # Into the Expert panel; raises ValueError when it does not fit the setup.
    api.imcontrol.setScanParameters(scan)
    api.imcontrol.setRecFilename(fileName)
    # "Scan once": starting the recording starts one scan; recordingEnded
    # comes once the scan is over and the file is written, recordingFailed
    # (a RuntimeError here) when the camera got no frames, say.
    callAndWaitForSignal(signals.recordingEnded, api.imcontrol.startRecording,
                         timeout=STACK_TIMEOUT_S, failSignal=signals.recordingFailed)
    print(f'  {fileName}')


def turnTo(angles):
    for mount, angle in zip(MOUNTS, angles):
        # Returns when the mount has arrived (the Elliptec manager waits).
        api.imcontrol.setRotatorPosition(mount, angle)
        arrived = api.imcontrol.getRotatorPosition(mount)
        if abs(arrived - angle) > ANGLE_TOLERANCE_DEG:
            raise RuntimeError(f'{mount} is at {arrived:.2f}°, not {angle:.2f}°')


def angleTag(angle):
    return f'{angle:05.1f}'.replace('.', 'p')     # 22.5 -> 022p5


scan = readScan(SCAN)
dark = readScan('dark_frames') if DARK_FRAMES_FIRST else None
print(f'{len(ANGLE_PAIRS_DEG)} stacks of {SCAN}, waveplates {MOUNTS[0]} and {MOUNTS[1]}')

# The scan the Expert panel holds now comes back afterwards; a panel that
# held none keeps the rig's scan.
panelHeldAScan = api.imcontrol.getScanParameters().get('program') is not None

api.imcontrol.setDetectorToRecord(CAMERA)
api.imcontrol.setRecModeScanOnce()
try:
    # The mounts and the panel go back to what they were, also when a stack
    # fails or Stop is pressed.
    with returningToPositions(rotators=list(MOUNTS)), \
            (temporaryScanParameters() if panelHeldAScan else contextlib.nullcontext()):
        if dark is not None:
            recordStack(dark, f'{NAME}_dark')
        for angles in ANGLE_PAIRS_DEG:
            turnTo(angles)
            recordStack(scan, f'{NAME}_{SCAN}_A{angleTag(angles[0])}_B{angleTag(angles[1])}')
finally:
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)

print(f'done: the stacks are in {api.imcontrol.getRecFolder()}')

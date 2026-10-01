"""Tutorial scanning 05 -- A laser power series in one recording.

You will learn
  * the laser API: ``getLaserNames()``, ``setLaserActive()``,
    ``setLaserValue()``
  * to run several scans into one until-stop recording, changing a
    setting between them
  * to leave the lasers off and the recording stopped whatever happens

Setup
  Mock setup:   galvo_apd_mock_scan_setup.json   <- a new setup
  It simulates: a point-scanning microscope -- galvo mirrors for X and Y
                and a Z piezo, driven by an NI-DAQ card -- with an APD
                ("APD") and two lasers: "405 (ON)", which can only be
                switched on and off, and "488 (EXC)", whose power is set as
                0-10 V.
  Your own microscope: needs an Advanced Scan widget, a point detector,
                and the Laser and Recording widgets. Change LASER and
                POWERS to your laser and its units (mW, %, V -- whatever
                the Laser widget shows). A value set on an on/off laser is
                ignored.

Next: that was the last tutorial. The workflows folder has complete
      acquisitions for real microscopes.
"""

import glob
import os
import tempfile

import h5py

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'point_scan_5um.json')
LASER = '488 (EXC)'
POWERS = [0.0, 1.0, 2.5, 5.0]         # in the laser's units: V on this setup
NAME = 'tutorial_power_series'
log = getLogger()
signals = api.imcontrol.signals()
print('Lasers:', api.imcontrol.getLaserNames())

backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
api.imcontrol.loadScanParamsFromFile(PARAMS)     # a 5 x 5 µm scan, 50 x 50 px
previousFormat = api.imcontrol.getRecFileFormat()
api.imcontrol.setRecFileFormat('HDF5')           # read back with h5py below
# One recording for the whole series: it runs until stopRecording(), and
# every scan in between adds one image to the same file.
api.imcontrol.setRecModeUntilStop()
api.imcontrol.setRecFilename(NAME)
try:
    callAndWaitForSignal(signals.recordingStarted, api.imcontrol.startRecording,
                         timeout=30)
    api.imcontrol.setLaserActive(LASER, True)      # switch the laser on
    for power in POWERS:
        api.imcontrol.setLaserValue(LASER, power)  # then set its power
        runScanAndWait(timeout=600)                # one scan = one image
        log.info(f'scan at {power} done')
finally:
    # Lasers first: whatever went wrong, the sample should not stay lit.
    api.imcontrol.setLaserValue(LASER, 0)
    api.imcontrol.setLaserActive(LASER, False)
    # Then end the recording, as in basic tutorial 05.
    waitForEnd = getWaitForSignal(signals.recordingEnded, timeout=60)
    if api.imcontrol.stopRecording():
        waitForEnd()
    else:
        waitForEnd.close()
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setRecFileFormat(previousFormat)
    api.imcontrol.loadScanParamsFromFile(backup)

path = max(glob.glob(os.path.join(api.imcontrol.getRecFolder(), f'{NAME}_rec_APD*')),
           key=os.path.getmtime)
with h5py.File(path, 'r') as f:
    images = f['APD/data']          # one image per scan, in scan order
    print(f'{os.path.basename(path)}: {images.shape[0]} scan images of '
          f'{images.shape[-1]} x {images.shape[-2]} px, one per power')

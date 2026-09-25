"""Tutorial scanning 05 -- A laser power series in one recording.

You will learn
  * the laser API: ``getLaserNames()``, ``setLaserActive()``,
    ``setLaserValue()``
  * to run several scans into one until-stop recording, changing a
    setting between them
  * to leave the lasers off and the recording stopped whatever happens

Setup
  Mock setup: galvo_apd_mock_scan_setup.json
  Needs:      an Advanced Scan widget, a point detector (APD), and the
              Laser and Recording widgets. On the mock, 488 (EXC) has an
              analog power channel (0-10 V); 405 (ON) is on/off only, and a
              value set on an on/off laser is ignored.

  This is the pattern of a real power series: change LASER and POWERS to
  your laser and its units (mW, %, V -- whatever the Laser widget shows).

Next: that is the last tutorial. The workflows folder has complete
      acquisitions for real rigs.
"""

import glob
import os
import tempfile

import h5py

PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'point_scan_5um.json')
LASER = '488 (EXC)'
POWERS = [0.0, 1.0, 2.5, 5.0]         # in the laser's units: V on the mock
NAME = 'tutorial_power_series'
log = getLogger()
signals = api.imcontrol.signals()
print('Lasers:', api.imcontrol.getLaserNames())

backup = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
api.imcontrol.saveScanParamsToFile(backup)
api.imcontrol.loadScanParamsFromFile(PARAMS)
previousFormat = api.imcontrol.getRecFileFormat()
api.imcontrol.setRecFileFormat('HDF5')         # read back with h5py below
api.imcontrol.setRecModeUntilStop()
api.imcontrol.setRecFilename(NAME)
try:
    callAndWaitForSignal(signals.recordingStarted, api.imcontrol.startRecording,
                         timeout=30)
    api.imcontrol.setLaserActive(LASER, True)
    for power in POWERS:
        api.imcontrol.setLaserValue(LASER, power)
        runScanAndWait(timeout=600)      # each scan adds one image to the file
        log.info(f'scan at {power} done')
finally:
    # Lasers first: whatever went wrong, the sample should not stay lit.
    api.imcontrol.setLaserValue(LASER, 0)
    api.imcontrol.setLaserActive(LASER, False)
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
    images = f['APD/data']
    print(f'{os.path.basename(path)}: {images.shape[0]} scan images of '
          f'{images.shape[-1]} x {images.shape[-2]} px, one per power')

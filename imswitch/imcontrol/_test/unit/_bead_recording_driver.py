"""Driver for test_bead_calibration_mock (run in a subprocess, see there).

Boots the real ImControl offscreen on the shipped ``simple_mock_setup`` with
the APD's virtual sample switched to the ``beads`` preset and the Advanced
scan panel (no SimplePointScan cloak), snaps the camera to HDF5 and Zarr
through the Recording panel, and records one Advanced scan of the APD
("Scan once") to HDF5 and to Zarr. Prints ``FILES <json>``.
"""
import glob
import json
import os
import sys
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('MPLBACKEND', 'Agg')

from imswitch.imcontrol._test.unit import _replay_e2e_driver as driver  # noqa: E402
from qtpy import QtTest  # noqa: E402

#: The Advanced scan recorded: X fast, Y slow, centre (3, -2) um.
SCAN = {'axis_length': [24.0, 20.0, 1.0], 'axis_step_size': [0.1, 0.1, 1.0],
        'axis_centerpos': [3.0, -2.0, 0.0]}


def _wait_for_file(rec, folder):
    for _ in range(1200):
        QtTest.QTest.qWait(100)
        done = [f for f in glob.glob(os.path.join(folder, '*')) if not f.endswith('.tmp')]
        if not rec.isRecording() and done:
            QtTest.QTest.qWait(300)
            return done[0]
    raise RuntimeError(f'no recording in {folder}')


def main(out):
    setup = driver.loadSetup('simple_mock_setup')
    setup['detectors']['APD']['managerProperties']['mockSample']['preset'] = 'beads'
    setup['scan'].pop('cloak', None)
    _, controller = driver.boot(setup, 'simple_mock_setup.json')
    controller.controllers['Laser'].setLaserValue('488 (EXC)', 1.0)
    controller.controllers['Laser'].setLaserActive('488 (EXC)', True)
    files = {'camera_h5': driver.snap(controller, os.path.join(out, 'cam_h5'), 'HDF5'),
             'camera_zarr': driver.snap(controller, os.path.join(out, 'cam_zarr'), 'ZARR')}
    scan = controller.controllers['Scan']
    params = scan.getScanParameters()
    params['analogParameterDict'].update({k: list(v) for k, v in SCAN.items()})
    scan.setScanParameters(params)
    rec = controller.controllers['Recording']
    rec.setDetectorToRecord('APD')
    for fmt in ('HDF5', 'ZARR'):
        folder = os.path.join(out, 'apd_' + fmt.lower())
        rec.setRecFolder(folder)
        rec.setRecFileFormat(fmt)
        rec.recScanOnce()
        rec.startRecording()
        files['apd_' + fmt.lower()] = _wait_for_file(rec, folder)
    print('FILES ' + json.dumps(files))


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except BaseException:
        sys.stderr.write(traceback.format_exc())
        raise

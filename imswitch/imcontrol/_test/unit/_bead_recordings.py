"""Bead recordings for the camera -> scan calibration tests.

:func:`record_mock_bead_files` runs ``_bead_recording_driver`` (the real
ImControl, offscreen, in a subprocess) to record a camera snap and an
Advanced scan of the APD on ``simple_mock_setup`` (virtual ``beads``
sample) to HDF5 and Zarr. The mock camera of that setup images noise, so
copies of the camera files get the same virtual sample rendered through a
known geometry (:data:`A`, :data:`T`: camera (row, col) -> (Y, X) um;
rotated 30 degrees, mirrored, 0.097 um per pixel).
"""
import json
import math
import os
import shutil
import site
import subprocess
import sys

import numpy as np

_DRIVER = 'imswitch.imcontrol._test.unit._bead_recording_driver'
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 4)))

#: The camera geometry the test renders: camera (row, col) -> sample (Y, X) um.
THETA = math.radians(30.0)
A = 0.097 * np.array([[math.cos(THETA), -math.sin(THETA)], [math.sin(THETA), math.cos(THETA)]]) \
    @ np.diag([1.0, -1.0])
T = np.array([-1.0, 2.0]) - A @ np.array([399.5, 399.5])
#: Points inside the recorded scan field (centre (Y, X) = (-2, 3) um, 20 x 24 um).
FIELD = np.array([[y, x] for y in np.linspace(-11, 7, 5) for x in np.linspace(-8, 14, 5)])


def _camera_image(shape=(800, 800)):
    from imswitch.imcontrol.model.managers.detectors._mock_sample import MockSample

    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    yx = np.stack([rows.ravel(), cols.ravel()], 1) @ A.T + T
    sample = MockSample(axes=(('X', 5.0), ('Y', 5.0)), preset='beads')
    brightness = sample.brightness(yx[:, 1], yx[:, 0], 0.15).reshape(shape)
    return np.random.default_rng(0).poisson(100 + 2000 * brightness).astype(np.uint16)


def record_mock_bead_files(out):
    env = dict(os.environ, HOME=str(out / 'home'), QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg',
               PYTHONUSERBASE=site.getuserbase(),
               PYTHONPATH=_REPO + os.pathsep + os.environ.get('PYTHONPATH', ''))
    os.makedirs(env['HOME'], exist_ok=True)
    proc = subprocess.run([sys.executable, '-m', _DRIVER, str(out)], env=env, cwd=_REPO,
                          capture_output=True, text=True, timeout=170)
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith('FILES '))
    paths = json.loads(line[len('FILES '):])
    image = _camera_image()
    import h5py
    import zarr

    beads_h5 = out / 'camera_beads.h5'
    shutil.copy(paths['camera_h5'], beads_h5)
    with h5py.File(beads_h5, 'r+') as handle:
        handle['Camera/data'][0] = image
    beads_zarr = out / 'camera_beads.zarr'
    shutil.copytree(paths['camera_zarr'], beads_zarr)
    zarr.open(str(beads_zarr), mode='r+')['Camera']['data'][0] = image
    paths.update(camera_beads_h5=str(beads_h5), camera_beads_zarr=str(beads_zarr), out=str(out))
    return paths



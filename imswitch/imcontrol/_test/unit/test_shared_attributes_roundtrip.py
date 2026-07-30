"""Regression test for the "Load parameters from saved HDF5/Zarr file" toolbar
action: structured ScanTTL metadata (dicts of per-device lists) must survive a
snap -> disk -> SharedAttributes round trip instead of being silently dropped
(the "Object dtype ... has no native HDF5 equivalent" bug) or never being read
back (the reader used to look for attrs directly on the detector group, but
the storers write them under a nested metadata/<category>/ subgroup)."""
import h5py
import numpy as np
import pytest
import zarr

from imswitch.imcommon.model import SharedAttributes
from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer


class _StubDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.2, 0.1]


class _StubDetectorManager:
    def __getitem__(self, name):
        return _StubDetector()


@pytest.fixture
def detman():
    return _StubDetectorManager()


# Reproduces the exact values from the reported bug: dicts of per-device lists,
# some empty, plus a plain scalar to make sure ordinary attrs still round-trip.
_SCAN_TTL_ATTRS = {
    'ScanTTL:linestep_enable': {'405 (ON)': [True], '488 (EXC)': [True], '488 (OFF)': [True]},
    'ScanTTL:pulse_starts_s': {'405 (ON)': [[]], '488 (EXC)': [[]], '488 (OFF)': [[]]},
    'ScanTTL:positioner_linestep_enable': {},
    'ScanTTL:advanced_device_lock_master': {'405 (ON)': False, '488 (EXC)': False},
    'Laser:power': 5.0,
}


def test_hdf5_scan_ttl_metadata_round_trips(detman, tmp_path):
    storer = HDF5Storer(str(tmp_path / 'snap'), detman)
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img}, {'Cam': dict(_SCAN_TTL_ATTRS)})

    with h5py.File(str(tmp_path / 'snap_Cam.h5'), 'r') as file:
        loaded = dict(SharedAttributes.fromHDF5File(file, 'Cam'))

    assert loaded[('ScanTTL', 'linestep_enable')] == _SCAN_TTL_ATTRS['ScanTTL:linestep_enable']
    assert loaded[('ScanTTL', 'pulse_starts_s')] == _SCAN_TTL_ATTRS['ScanTTL:pulse_starts_s']
    assert loaded[('ScanTTL', 'positioner_linestep_enable')] == {}
    assert loaded[('ScanTTL', 'advanced_device_lock_master')] == \
        _SCAN_TTL_ATTRS['ScanTTL:advanced_device_lock_master']
    assert loaded[('Laser', 'power')] == 5.0


def test_zarr_scan_ttl_metadata_round_trips(detman, tmp_path):
    storer = ZarrStorer(str(tmp_path / 'snap'), detman)
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({'Cam': img}, {'Cam': dict(_SCAN_TTL_ATTRS)})

    root = zarr.open(str(tmp_path / 'snap.zarr'), mode='r')
    loaded = dict(SharedAttributes.fromZarrStore(root, 'Cam'))

    assert loaded[('ScanTTL', 'linestep_enable')] == _SCAN_TTL_ATTRS['ScanTTL:linestep_enable']
    assert loaded[('ScanTTL', 'pulse_starts_s')] == _SCAN_TTL_ATTRS['ScanTTL:pulse_starts_s']
    assert loaded[('ScanTTL', 'positioner_linestep_enable')] == {}
    assert loaded[('ScanTTL', 'advanced_device_lock_master')] == \
        _SCAN_TTL_ATTRS['ScanTTL:advanced_device_lock_master']
    assert loaded[('Laser', 'power')] == 5.0

"""Replay metadata through every storer, and what must never be recorded.

The setup identity (``setup:*``, ``metadata:schema_version``), the laser
units/range next to each value and the replay provenance (``replay:*``) must
survive HDF5 and Zarr back into :class:`SharedAttributes` -- where replay
reads them -- and reach an OME-TIFF as annotations, the only place an
OME-TIFF has for them.  The inventory must hold no serial number, port or
path.
"""
import json
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
import tifffile
import zarr

from imswitch.imcommon.model import SharedAttributes
from imswitch.imcontrol.model import replay, replay_inventory
from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer, TiffStorer, ZarrStorer, annotationsFromAttrs,
)
from imswitch.imcontrol.model.managers.recording_metadata import MODE_SNAP, build_ome_image_meta


class _DetMan:
    def __getitem__(self, name):
        return SimpleNamespace(dtype=np.dtype(np.uint16), pixelSizeUm=[1.0, 0.1, 0.1])


INVENTORY = {
    'schema': 1,
    'lasers': {'488': {'managerClass': 'X', 'wavelength': 488, 'valueUnits': 'mW',
                       'valueRangeMin': 0, 'valueRangeMax': 200, 'isBinary': False}},
    'positioners': {'Stage': {'axes': ['X', 'Y'], 'axisRanges': {'X': [0, 100]}}},
    'detectors': {}, 'rotators': {'hwp': {'managerClass': 'R'}}, 'flipMirrors': {},
    'pulseGenerators': {}, 'scan': None,
}
MAPPING = json.dumps([{'id': 'laser:Old', 'target': '488', 'applied': {'Value': 5.0},
                       'skipped': {}, 'failed': {}}])
ATTRS = {
    'metadata:schema_version': 1,
    'setup:name': 'rig.json', 'setup:hash': 'sha256:abc', 'setup:inventory': INVENTORY,
    'Laser:488:Value': 5.0, 'Laser:488:Units': 'mW', 'Laser:488:RangeMin': 0,
    'Laser:488:RangeMax': 200,
    'Rotator:hwp:Position': 45.0, 'FlipMirror:fm1:State': 1,
    'PulseGenerator:teensyPulse:Connected': True,
    'replay:source_file': 'old.h5', 'replay:source_setup_hash': 'sha256:old',
    'replay:mapping': MAPPING,
}


def _check(loaded):
    assert loaded[('metadata', 'schema_version')] == 1
    assert loaded[('setup', 'hash')] == 'sha256:abc'
    assert loaded[('setup', 'inventory')] == INVENTORY
    assert loaded[('Laser', '488', 'Units')] == 'mW'
    assert loaded[('Laser', '488', 'RangeMax')] == 200
    assert loaded[('Rotator', 'hwp', 'Position')] == 45.0
    assert loaded[('replay', 'mapping')] == MAPPING
    parsed = replay.parseReplayAttributes(loaded)
    assert parsed.inventory == INVENTORY and parsed.schemaVersion == 1
    assert parsed.rotators == {'hwp': {'Position': 45.0}}
    assert parsed.flipMirrors == {'fm1': {'State': 1}}


def test_hdf5_round_trip(tmp_path):
    HDF5Storer(str(tmp_path / 'snap'), _DetMan()).snap(
        {'Cam': np.zeros((8, 8), np.uint16)}, {'Cam': dict(ATTRS)})
    with h5py.File(str(tmp_path / 'snap_Cam.h5'), 'r') as file:
        _check(dict(SharedAttributes.fromHDF5File(file, 'Cam')))


def test_zarr_round_trip(tmp_path):
    ZarrStorer(str(tmp_path / 'snap'), _DetMan()).snap(
        {'Cam': np.zeros((8, 8), np.uint16)}, {'Cam': dict(ATTRS)})
    root = zarr.open(str(tmp_path / 'snap.zarr'), mode='r')
    _check(dict(SharedAttributes.fromZarrStore(root, 'Cam')))


def test_ome_tiff_carries_setup_identity_and_replay_provenance(tmp_path):
    annotations = annotationsFromAttrs(dict(ATTRS))
    assert annotations['setup:hash'] == 'sha256:abc'
    assert annotations['replay:source_file'] == 'old.h5'
    assert 'Laser:488:Value' not in annotations
    storer = TiffStorer(str(tmp_path / 'snap'), _DetMan())
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.1, 0.1), dtype=np.uint16,
        annotations=annotations)}
    storer.snap({'Cam': np.zeros((8, 8), np.uint16)})
    with tifffile.TiffFile(str(tmp_path / 'snap_Cam.ome.tiff')) as t:
        ome = t.ome_metadata or ''
    assert 'sha256:abc' in ome and 'old.h5' in ome and 'replay:mapping' in ome


def test_annotations_stay_empty_without_notes_or_provenance():
    assert annotationsFromAttrs({'Laser:488:Value': 1.0}) == {}


# ---------------------------------------------------------- sensitive data

class _Managers(dict):
    def __iter__(self):
        return iter(list(self.items()))


def _sensitiveMaster():
    laser = SimpleNamespace(wavelength=488, valueUnits='mW', valueRangeMin=0,
                            valueRangeMax=100, valueRangeStep=1, isBinary=False,
                            isModulated=False, port='COM7', serialNumber='SN-123456')
    info = SimpleNamespace(managerProperties={'posRangeUm': [0, 50], 'port': 'COM8',
                                              'serial': 'SN-999'})
    positioner = SimpleNamespace(axes=['Z'], forScanning=True, forPositioning=False,
                                 _positionerInfo=info, port='COM8')
    detector = SimpleNamespace(model='Cam', fullShape=(10, 10), supportedBinnings=[1],
                               forAcquisition=True, configOwnedParameters=frozenset(),
                               parameters={}, serialNumber='SN-777',
                               cameraListIndex=0)
    pulse = SimpleNamespace(n_digital_channels=8, jitter_ns=1, min_pulse_width_ns=1,
                            supports_analog=False, port='/dev/ttyACM0')
    master = SimpleNamespace(
        lasersManager=_Managers({'488': laser}),
        positionersManager=_Managers({'Z': positioner}),
        detectorsManager=_Managers({'Cam': detector}),
        rotatorsManager=_Managers(), flipMirrorsManager=_Managers(),
        pulseGeneratorManager=pulse)
    setupInfo = SimpleNamespace(
        flipMirrors=None, scan=None,
        teensyPulse=SimpleNamespace(port='/dev/ttyACM0', pinMap={'488': 3}))
    return master, setupInfo


def _allKeys(value, out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _allKeys(v, out)
    elif isinstance(value, list):
        for v in value:
            _allKeys(v, out)
    return out


def test_inventory_holds_no_serials_ports_or_paths():
    master, setupInfo = _sensitiveMaster()
    inventory = replay_inventory.buildInventory(master, setupInfo)
    text = json.dumps(inventory)
    for secret in ('COM7', 'COM8', 'SN-123456', 'SN-999', 'SN-777', '/dev/ttyACM0'):
        assert secret not in text
    assert not [k for k in _allKeys(inventory) if replay.isSensitiveField(k)]
    # ... while keeping what remapping needs.
    assert inventory['lasers']['488']['wavelength'] == 488
    assert inventory['positioners']['Z']['axisRanges'] == {'Z': [0, 50]}
    assert inventory['pulseGenerators']['teensyPulse']['pinMap'] == {'488': 3}


def test_sanitize_drops_sensitive_fields_a_future_builder_might_add():
    dirty = {'lasers': {'a': {'wavelength': 1, 'serialNumber': 'x', 'comPort': 'COM1'}}}
    assert replay.sanitizeInventory(dirty) == {'lasers': {'a': {'wavelength': 1}}}


def test_inventory_size_is_small(tmp_path):
    """File-size impact: the setup metadata of a rig with a few devices is a
    few kilobytes, recorded once per detector group."""
    master, setupInfo = _sensitiveMaster()
    inventory = replay_inventory.buildInventory(master, setupInfo)
    assert len(json.dumps(inventory)) < 4096

"""Aborting scanN must not erase earlier scientific timepoints in one Zarr."""

from types import SimpleNamespace

import numpy as np
import pytest

zarr = pytest.importorskip('zarr')
pytest.importorskip('qtpy')

from imswitch.imcontrol.model import SaveMode
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.execution.readiness import (
    ReadinessState, PhysicalReference, evaluate_evidence, inspect_physical_reference,
)


def _open(path, *, index, shared):
    detectors = {'CAM': SimpleNamespace(
        dtype=np.dtype('uint16'), pixelSizeUm=(0.1, 0.1),
    )}
    storer = ZarrStorer(str(path), detectors)
    names = ['CAM']
    storer.openStream(
        fileDests={'CAM': str(path)}, detectorNames=names,
        shapes={'CAM': (3, 4)},
        attrs={'CAM': {
            'recording:lapse_index': index,
            'recording:num_timepoints': 5,
            'recording:single_lapse_file': shared,
            'recording:planned_frames': 2,
        }},
        singleMultiDetectorFile=False, singleLapseFile=shared,
        saveMode=SaveMode.Disk,
    )
    return storer


def _write(storer, path, *, finish):
    frames = np.full((2 if finish else 1, 3, 4), 17, dtype=np.uint16)
    storer.writeFrames('CAM', frames)
    if finish:
        storer.finalizeStream({'CAM': 2}, {'CAM': str(path)}, None, SaveMode.Disk)
    else:
        storer.abortStream({'CAM': str(path)}, {'CAM': str(path)}, SaveMode.Disk)


def test_shared_zarr_abort_preserves_sealed_prefix_and_ngff_index(tmp_path):
    path = tmp_path / 'lapse.zarr'
    for t in (0, 1):
        _write(_open(path, index=t, shared=True), path, finish=True)

    unfinished = _open(path, index=2, shared=True)
    unfinished.writeFrames('CAM', np.ones((1, 3, 4), dtype=np.uint16))
    unfinished.abortStream({'CAM': str(path)}, {'CAM': str(path)}, SaveMode.Disk)
    root = zarr.open_group(str(path), mode='r')
    assert 'scan0' in root and 'scan1' in root and 'scan2' not in root
    assert int(root['scan0/CAM/data'][0, 0, 0]) == 17
    assert int(root['scan1/CAM/data'][1, 0, 0]) == 17
    paths = [entry['path'] for entry in root.attrs['ome']['series']]
    assert paths == ['scan0/CAM', 'scan1/CAM']
    for t in (0, 1):
        ref = PhysicalReference(str(path), f'scan{t}/CAM', t)
        state = evaluate_evidence(
            inspect_physical_reference(ref), is_lapse=True,
            expected_total=5, logical_index=t,
        )
        assert state.state == ReadinessState.READY


def test_multifile_zarr_abort_only_discards_active_store(tmp_path):
    first = tmp_path / 'rec_scan0_CAM.zarr'
    second = tmp_path / 'rec_scan1_CAM.zarr'
    _write(_open(first, index=0, shared=False), first, finish=True)
    _write(_open(second, index=1, shared=False), second, finish=False)
    assert first.is_dir()
    assert not second.exists()
    root = zarr.open_group(str(first), mode='r')
    assert tuple(root['CAM/data'].shape) == (2, 3, 4)


def test_new_shared_zarr_with_only_aborted_scan_is_removed(tmp_path):
    path = tmp_path / 'unfinished.zarr'
    _write(_open(path, index=0, shared=True), path, finish=False)
    assert not path.exists()

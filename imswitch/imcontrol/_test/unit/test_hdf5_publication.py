"""Publication tests: do not open a growing HDF5 file through its public name."""

import os

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.hdf5_publication import (
    Hdf5PublicationError,
    Hdf5PublicationRegistry,
)


def _stage_group(path, index):
    with h5py.File(path, 'a', libver='latest') as handle:
        handle.create_group(f'scan{index}').create_dataset(
            'Camera/data', data=np.full((2, 4, 5), index, dtype=np.uint16)
        )


def test_multifile_timepoints_are_independently_published(tmp_path):
    publication = Hdf5PublicationRegistry()
    for index in range(3):
        public = str(tmp_path / f'rec_scan{index}_CAM.hdf5')
        working = publication.reserve(public)
        _stage_group(working, index)
        assert not os.path.exists(public)
        assert os.path.isfile(working)
        assert not publication.is_finalized(public)
        publication.mark_finalized([public])
        assert publication.publish([public]) == (public,)
        assert os.path.isfile(public)
        assert not os.path.exists(working)
        with h5py.File(public, 'r') as handle:
            np.testing.assert_array_equal(
                handle[f'scan{index}/Camera/data'][:], index
            )


def test_single_file_remains_staged_until_explicit_lapse_terminal(tmp_path):
    publication = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = publication.reserve(public)
    for index in (0, 1):
        _stage_group(working, index)
        publication.mark_finalized([public], deferred=True)
        assert not os.path.exists(public), 'writer terminal must not publish run'
    # User stopped a 5-timepoint lapse after two valid timepoints.
    assert publication.publish_pending_finalized() == (public,)
    assert publication.publish_pending_finalized() == ()
    with h5py.File(public, 'r') as handle:
        assert sorted(handle.keys()) == ['scan0', 'scan1']


def test_stale_partial_or_public_file_prevents_reuse(tmp_path):
    publication = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = publication.reserve(public)
    assert not publication.available(public)
    with pytest.raises(FileExistsError):
        publication.reserve(public)
    # Even if another process restarts, the staged filename is reserved by
    # the existing disk file, not only an in-memory registry.
    _stage_group(working, 0)
    restarted = Hdf5PublicationRegistry()
    assert not restarted.available(public)
    publication.mark_finalized([public])
    publication.publish([public])
    assert not restarted.available(public)


def test_publication_failure_retains_finalized_hdf5_for_recovery(tmp_path, monkeypatch):
    publication = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = publication.reserve(public)
    _stage_group(working, 0)
    publication.mark_finalized([public])

    def denied(*_args):
        raise PermissionError('simulated Windows publish denial')

    syscall = 'rename' if os.name == 'nt' else 'link'
    with monkeypatch.context() as patcher:
        patcher.setattr(os, syscall, denied)
        with pytest.raises(Hdf5PublicationError, match='retained'):
            publication.publish([public])
    assert not os.path.exists(public)
    assert os.path.isfile(working)
    assert publication.is_finalized(public)
    publication.publish([public])
    assert os.path.exists(public)


def test_publication_never_overwrites_existing_final_path(tmp_path):
    publication = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = publication.reserve(public)
    _stage_group(working, 0)
    publication.mark_finalized([public])
    with open(public, 'wb') as handle:
        handle.write(b'existing data must not be replaced')
    with pytest.raises(Hdf5PublicationError, match='retained'):
        publication.publish([public])
    with open(public, 'rb') as handle:
        assert handle.read() == b'existing data must not be replaced'
    assert os.path.isfile(working)


def test_unfinalized_abort_cannot_publish(tmp_path):
    publication = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = publication.reserve(public)
    _stage_group(working, 0)
    with pytest.raises(Hdf5PublicationError, match='before writer finalization'):
        publication.publish([public])
    os.unlink(working)
    publication.discard_missing_unfinalized()
    assert publication.available(public)


def test_claim_reserves_name_across_process_registries_before_file_exists(tmp_path):
    first = Hdf5PublicationRegistry()
    second = Hdf5PublicationRegistry()
    public = str(tmp_path / 'rec_CAM.hdf5')
    working = first.reserve(public)
    assert not os.path.exists(working)
    assert not second.available(public)
    with pytest.raises(FileExistsError):
        second.reserve(public)
    # A failed initial recording never created a file: cleanup removes only
    # its *own* reservation and the name can be selected again.
    first.discard_missing_unfinalized()
    assert second.available(public)
    assert second.reserve(public) == working


def test_stopped_shared_lapse_does_not_publish_unrelated_failed_multi_file(tmp_path):
    publication = Hdf5PublicationRegistry()
    old = str(tmp_path / 'old_scan0_CAM.hdf5')
    current = str(tmp_path / 'current_CAM.hdf5')
    _stage_group(publication.reserve(old), 0)
    publication.mark_finalized([old])  # failed ordinary per-file publication
    _stage_group(publication.reserve(current), 0)
    publication.mark_finalized([current], deferred=True)

    assert publication.publish_pending_finalized() == (current,)
    assert os.path.isfile(current)
    assert not os.path.isfile(old)
    assert os.path.isfile(publication.working_path(old))


def test_publish_run_terminal_is_scoped_to_its_public_path(tmp_path):
    publication = Hdf5PublicationRegistry()
    old = str(tmp_path / 'old_CAM.hdf5')
    current = str(tmp_path / 'current_CAM.hdf5')
    for public in (old, current):
        _stage_group(publication.reserve(public), 0)
        publication.mark_finalized([public], deferred=True)
    assert publication.publish_pending_finalized([current]) == (current,)
    assert not os.path.exists(old)
    assert publication.is_finalized(old)
    assert publication.publish_pending_finalized([old]) == (old,)


def test_aborted_active_point_preserves_earlier_finalized_shared_data(tmp_path):
    registry = Hdf5PublicationRegistry()
    public = str(tmp_path / 'lapse_CAM.hdf5')
    working = registry.reserve(public)
    _stage_group(working, 0)
    registry.mark_finalized([public], deferred=True)
    # Simulate the later point opening/appending before the user presses STOP.
    # The current HDF5 abort rule intentionally retains any earlier groups.
    with h5py.File(working, 'a') as handle:
        handle.create_group('scan1')
    registry.publish_pending_finalized([public])
    with h5py.File(public, 'r') as handle:
        assert 'scan0/Camera/data' in handle
        assert 'scan1' in handle
        assert handle['scan0/Camera/data'].shape == (2, 4, 5)

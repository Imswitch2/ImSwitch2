"""The completion-outcome attribute is as wide as its vocabulary, never truncated.

It was created as a literal ``S13`` -- the length of the longer of the two
outcomes that existed -- and ``attrs.modify`` cuts a longer value to the
attribute's width without a word, so a future ``stopped_early_on_stall``
would have been stored as a valid, wrong ``stopped_early``.
"""

import h5py
import numpy as np

from imswitch.imcommon.model.acquisition_metadata import VALID_COMPLETION_OUTCOMES
from imswitch.imcontrol.model.managers.RecordingManager import (
    COMPLETION_OUTCOME_ATTR_DTYPE, HDF5Storer,
)


def test_the_attribute_width_follows_the_vocabulary():
    longest = max(len(value) for value in VALID_COMPLETION_OUTCOMES)
    assert COMPLETION_OUTCOME_ATTR_DTYPE == f"S{longest}"
    assert np.dtype(COMPLETION_OUTCOME_ATTR_DTYPE).itemsize == longest


def test_a_longer_value_widens_the_attribute_instead_of_being_cut(tmp_path):
    path = str(tmp_path / 'outcome.h5')
    with h5py.File(path, 'w') as f:
        dataset = f.create_dataset('data', data=np.zeros((2, 2), dtype=np.uint16))
        dataset.attrs.create('recording:completion_outcome', np.bytes_(''), dtype='S13')
        HDF5Storer._set_hdf5_attr(dataset, 'recording:completion_outcome',
                                  'stopped_early_on_stall', 'recording')
    with h5py.File(path, 'r') as f:
        stored = f['data'].attrs['recording:completion_outcome']
        stored = stored.decode() if isinstance(stored, bytes) else str(stored)
    assert stored == 'stopped_early_on_stall'

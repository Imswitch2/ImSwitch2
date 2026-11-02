"""MoNaLISA's streaming contract: the session begins on one whole scan stack,
which the grid localization and the scan orientation detection need.

    pytest imswitch/improcess/_test/test_monalisa_contract.py -v
"""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.base import Incompatible, RawInfo
from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor


def _info(S):
    return RawInfo(T=20, S=S, frame_shape=(540, 540), dtype=np.dtype(np.int16), mode="ScanLapse")


def test_the_init_frames_are_one_whole_scan_stack():
    contract = MonalisaReconstructor.contract(_info((576,)), {"device": "GPU"})

    assert contract.init_frames == 576
    assert contract.chunk_frames == 1
    assert contract.params == {"device": "GPU"}


def test_a_recording_without_scan_stacks_is_refused():
    with pytest.raises(Incompatible):
        MonalisaReconstructor.contract(_info(()), {})

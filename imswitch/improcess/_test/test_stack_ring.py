"""Standalone tests for ``imswitch.improcess.live.buffer.StackRing``.

Pure data-structure tests -- no threads, no Qt. Run either with pytest::

    pytest imswitch/improcess/_test/test_stack_ring.py -v

or directly::

    python imswitch/improcess/_test/test_stack_ring.py
"""

import numpy as np
import pytest

from imswitch.improcess.live.buffer import StackRing


FRAME_SHAPE = (3, 2)
DTYPE = np.uint16


def _frame(value: int, shape=FRAME_SHAPE, dtype=DTYPE) -> np.ndarray:
    """A frame whose every pixel equals ``value``."""
    return np.full(shape, value, dtype=dtype)


# --- construction / validation ----------------------------------------------

def test_init_exposes_geometry():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    assert ring.num_frames == 4
    assert tuple(ring.frame_shape) == FRAME_SHAPE
    assert ring.nbytes == 4 * 3 * 2 * np.dtype(DTYPE).itemsize


@pytest.mark.parametrize("bad", [0, -1])
def test_init_rejects_non_positive_num_frames(bad):
    with pytest.raises(ValueError):
        StackRing(bad, FRAME_SHAPE, DTYPE)


def test_init_rejects_non_positive_frame_dim():
    with pytest.raises(ValueError):
        StackRing(4, (0, 2), DTYPE)


# --- write / read round-trip ----------------------------------------------

def test_single_frame_roundtrip():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    for i in range(4):
        ring.write(i, _frame(i))

    for i in range(4):
        got = ring.read(i)
        assert got.shape == (1, *FRAME_SHAPE)
        assert np.array_equal(got[0], _frame(i))


def test_write_copies_frame_in():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    src = _frame(5)
    ring.write(0, src)
    src[:] = 99                          # mutate the source after write
    assert np.array_equal(ring.read(0)[0], _frame(5))


def test_read_returns_a_view_not_a_copy():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    ring.write(0, _frame(1))
    got = ring.read(0)
    # Pins the zero-copy contract -- the consumer must consume before the slot
    # is next written.
    assert np.shares_memory(got, ring._mem_ring)


# --- global-index addressing (statelessness) -----------------------------

def test_index_wraps_modulo_num_frames():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    ring.write(4, _frame(42))            # global 4 -> slot 0
    assert np.array_equal(ring.read(4)[0], _frame(42))


def test_next_stack_reuses_buffer_without_any_reset():
    ring = StackRing(4, FRAME_SHAPE, DTYPE)
    for g in range(4):                   # stack 0: global 0..3
        ring.write(g, _frame(100 + g))

    for g in range(4, 8):                # stack 1: global 4..7 -> slots 0..3
        ring.write(g, _frame(200 + (g - 4)))

    for g in range(4, 8):
        assert np.array_equal(ring.read(g)[0], _frame(200 + (g - 4)))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

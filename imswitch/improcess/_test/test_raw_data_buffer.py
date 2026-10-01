"""Tests for ``RawDataBuffer`` and ``FrameGate``.

The buffer is a plain array; the gate is the part with threads in it, so it is
exercised with a real producer and consumer rather than by inspection.

    pytest imswitch/improcess/_test/test_raw_data_buffer.py -v
"""

import threading

import numpy as np
import pytest

from imswitch.improcess.live.buffer import (
    MIN_BUFFER_FRAMES,
    FrameGate,
    RawDataBuffer,
)

FRAME_SHAPE = (4, 5)
DTYPE = np.float32


# --- sizing for the job -----------------------------------------------------

def test_a_scan_gets_a_whole_timepoint():
    """Big enough already; the floor changes nothing."""
    buffer = RawDataBuffer.for_job(1225, FRAME_SHAPE, DTYPE)

    assert buffer.capacity == 1225


def test_a_single_frame_timepoint_gets_the_floor_not_one_slot():
    """A one-slot buffer would make the producer wait for the consumer after
    every single frame -- which is what a camera recording would have got."""
    buffer = RawDataBuffer.for_job(1, FRAME_SHAPE, DTYPE)

    assert buffer.capacity == MIN_BUFFER_FRAMES


def test_a_timepoint_always_fits():
    """The consumer is told a stack is done only once every frame of it has
    been pushed, so a shorter buffer could recycle a slot it has not reached."""
    for frames_per_stack in (1, 7, 33, 1225):
        buffer = RawDataBuffer.for_job(frames_per_stack, FRAME_SHAPE, DTYPE)
        assert buffer.capacity >= frames_per_stack


def test_the_floor_is_adjustable():
    assert RawDataBuffer.for_job(1, FRAME_SHAPE, DTYPE, min_capacity=4).capacity == 4


# --- the store itself -------------------------------------------------------

def test_geometry_is_exposed():
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)

    assert buffer.capacity == 4
    assert buffer.frame_shape == FRAME_SHAPE
    assert buffer.dtype == np.dtype(DTYPE)
    assert buffer.nbytes == 4 * 4 * 5 * 4


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_capacity_is_rejected(bad):
    with pytest.raises(ValueError):
        RawDataBuffer(bad, FRAME_SHAPE, DTYPE)


def test_a_non_positive_frame_dimension_is_rejected():
    with pytest.raises(ValueError):
        RawDataBuffer(4, (0, 2), DTYPE)


def test_a_frame_survives_the_round_trip():
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)
    frame = np.arange(20, dtype=DTYPE).reshape(FRAME_SHAPE)

    buffer.write(2, frame)

    np.testing.assert_array_equal(buffer.read(2)[0], frame)


def test_the_index_wraps_modulo_capacity():
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)
    buffer.write(0, np.full(FRAME_SHAPE, 7, DTYPE))

    np.testing.assert_array_equal(buffer.read(4)[0], 7)   # same slot


def test_read_returns_a_view_not_a_copy():
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)
    buffer.write(0, np.full(FRAME_SHAPE, 1, DTYPE))

    got = buffer.read(0)

    # Pins the zero-copy contract -- the consumer must consume before the slot
    # is next written.
    assert np.shares_memory(got, buffer._buffer)


def test_the_next_pass_reuses_the_slots_without_any_reset():
    """Stateless: nothing is cleared between passes, the index just wraps."""
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)
    for index in range(4):
        buffer.write(index, np.full(FRAME_SHAPE, 100 + index, DTYPE))

    for index in range(4, 8):
        buffer.write(index, np.full(FRAME_SHAPE, 200 + index - 4, DTYPE))

    for index in range(4, 8):
        np.testing.assert_array_equal(
            buffer.read(index)[0], np.full(FRAME_SHAPE, 200 + index - 4, DTYPE)
        )


# --- the gate ---------------------------------------------------------------

def test_nothing_reported_means_nothing_is_reached():
    gate = FrameGate()

    assert gate.reached(0) is False
    assert gate.position == -1


def test_a_frame_is_reached_once_reported():
    gate = FrameGate()
    gate.consumed_through(4)

    assert gate.reached(4) is True
    assert gate.reached(3) is True      # everything up to it, too
    assert gate.reached(5) is False


def test_progress_never_rewinds():
    """A late or duplicated report must not free a slot twice."""
    gate = FrameGate()
    gate.consumed_through(9)
    gate.consumed_through(3)

    assert gate.position == 9


def test_release_lets_every_waiter_through():
    gate = FrameGate()
    assert gate.reached(100) is False

    gate.release()

    assert gate.released
    assert gate.reached(100) is True
    assert gate.wait_until(100, timeout=0) is True


def test_wait_returns_false_while_the_frame_is_outstanding():
    gate = FrameGate()

    assert gate.wait_until(4, timeout=0.01) is False


def test_wait_wakes_as_soon_as_the_consumer_reports():
    gate = FrameGate()
    woke = threading.Event()

    def producer():
        if gate.wait_until(4, timeout=2.0):
            woke.set()

    thread = threading.Thread(target=producer)
    thread.start()
    gate.consumed_through(4)
    thread.join(timeout=2.0)

    assert woke.is_set()


def test_a_producer_never_laps_its_consumer():
    """The property the whole thing exists for: a producer running flat out
    against a slower consumer never overwrites a frame before it is read.

    Modelled as the workers do it -- a queue stands in for ``sigFramesReady``,
    and the producer tracks which frame owns each slot so it waits for that one
    rather than deriving it from the index.
    """
    import queue

    capacity, total = 8, 300
    buffer = RawDataBuffer(capacity, (2, 2), np.int64)
    gate = FrameGate()
    ready: "queue.Queue[int]" = queue.Queue()
    seen, wrong = [], []

    def consume():
        for _ in range(total):
            index = ready.get()
            value = int(buffer.read(index)[0][0, 0])
            if value != index:
                wrong.append((index, value))
            seen.append(index)
            gate.consumed_through(index)      # only now is the slot free

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()

    slot_owner: list[int | None] = [None] * capacity
    for index in range(total):
        owner = slot_owner[index % capacity]
        while owner is not None and not gate.wait_until(owner, timeout=1.0):
            pass
        buffer.write(index, np.full((2, 2), index, np.int64))
        slot_owner[index % capacity] = index
        ready.put(index)

    consumer.join(timeout=10.0)

    assert not wrong, f"frames overwritten before being read: {wrong[:5]}"
    assert seen == list(range(total))


def test_a_skipped_timepoint_does_not_strand_the_producer():
    """The regression: a timelapse that skips t3 jumps the global index, so a
    slot's next occupant is not 'index - capacity' frames later. Deriving it
    arithmetically leaves the producer waiting on frames that are never
    produced, and live mode stops advancing."""
    capacity, frames_per_stack = 4, 4
    gate = FrameGate()
    slot_owner: list[int | None] = [None] * capacity

    for index in range(3 * frames_per_stack):        # t0, t1, t2
        slot_owner[index % capacity] = index
        gate.consumed_through(index)

    # t3 never arrived; the source resumes at t4 -> global index 16.
    owner = slot_owner[16 % capacity]

    assert owner == 12 - 4                            # slot 0 last held frame 8
    assert gate.reached(owner) is True                # consumed long ago
    # The arithmetic rule would have asked for frame 12, which never existed:
    assert gate.reached(16 - capacity) is False


def test_the_buffer_alone_recycles_a_slot_under_its_reader():
    """Shows the test above is measuring something, without racing to prove it.

    The store has no protection of its own: a read view aliases the slot, and
    writing capacity frames later replaces what the reader is looking at. That
    is the whole reason the gate exists, and asserting it deterministically
    beats asserting that two threads happen to collide.
    """
    buffer = RawDataBuffer(4, FRAME_SHAPE, DTYPE)
    buffer.write(0, np.full(FRAME_SHAPE, 1, DTYPE))
    view = buffer.read(0)

    buffer.write(4, np.full(FRAME_SHAPE, 2, DTYPE))   # same slot, gate unconsulted

    np.testing.assert_array_equal(view[0], 2)          # changed under the reader


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

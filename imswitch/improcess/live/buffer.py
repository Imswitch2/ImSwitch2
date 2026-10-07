"""Frame store shared between the live stream and process workers.

``RawDataBuffer`` is a pure, fixed, **stateless** structure: a preallocated
``(capacity, *frame_shape)`` array and nothing else. It holds the frames
currently in flight, addressed by **global frame index**; slot ``i`` holds
whichever frame has index ``i (mod capacity)``.

  * the stream worker calls :meth:`RawDataBuffer.write` (one frame, by index)
    then emits that index on a Qt signal,
  * the process worker calls :meth:`RawDataBuffer.read` (same index) to get the
    frame by reference and hand it to ``session.push``.

``capacity`` is the buffer's own business and is chosen per job -- see
:meth:`RawDataBuffer.for_job`. A scan needs a whole timepoint in flight; a
camera recording contributing one frame per timepoint does not, and sizing its
buffer to one frame would make producer and consumer run in lock-step.

Keeping the producer off the consumer's heels is :class:`FrameGate`'s job, not
the buffer's. The buffer keeps no epoch or tag, so a :meth:`RawDataBuffer.read`
for a slot the producer has already recycled silently returns the newer frame
-- correctness rests entirely on the gate.
"""

import threading

import numpy as np


#: Smallest buffer worth allocating. A job whose timepoint is a single frame
#: would otherwise get a one-slot buffer, forcing the producer to wait for the
#: consumer after every frame; these are a few hundred kB at typical frame
#: sizes and buy the producer room to run.
MIN_BUFFER_FRAMES = 32


class RawDataBuffer:
    """Frames in flight between the stream and process workers."""

    def __init__(
        self,
        capacity: int,
        frame_shape: tuple[int, ...],
        dtype,
    ) -> None:
        """Allocate the frame store.

        Args:
            capacity: Number of slots. The producer may run this many frames
                ahead of the consumer before it must wait.
            frame_shape: Shape of a single raw frame (typically ``(H, W)``).
            dtype: Frame dtype (anything ``np.dtype`` accepts).

        Raises:
            ValueError: ``capacity < 1`` or ``frame_shape`` has a non-positive
                dimension.
        """
        if capacity < 1:
            raise ValueError("The expected number of frames to be stored is less than 1.")

        if any(d <= 0 for d in frame_shape):
            raise ValueError("A dimension in the frame shape is non-positive")

        self._capacity = int(capacity)
        self._frame_shape = tuple(frame_shape)
        self._dtype = np.dtype(dtype)
        self._buffer = np.empty((self._capacity, *self._frame_shape), self._dtype)

    @classmethod
    def for_job(
        cls,
        frames_per_stack: int,
        frame_shape: tuple[int, ...],
        dtype,
        *,
        min_capacity: int = MIN_BUFFER_FRAMES,
    ) -> "RawDataBuffer":
        """Allocate a buffer shaped for one job.

        A timepoint must fit whole: the process worker is told a stack is done
        only once every frame of it has been pushed, so a buffer smaller than
        ``frames_per_stack`` could recycle a slot the consumer has not reached.

        Past that, ``min_capacity`` is a floor rather than a target. A scan
        already asks for hundreds of frames and is unaffected; a camera
        recording asks for one, and the floor is what stops producer and
        consumer marching in lock-step for the whole run.
        """
        capacity = max(int(frames_per_stack or 1), int(min_capacity))
        return cls(capacity, frame_shape, dtype)

    def write(self, index: int, frame: np.ndarray) -> None:
        """Copy one raw frame into slot ``index % capacity``.

        Args:
            index: Global frame index -- the buffer maps it to a slot with
                ``% capacity``.
            frame: Frame data, shape ``frame_shape``.
        """
        self._buffer[index % self._capacity] = frame

    def read(self, index: int) -> np.ndarray:
        """Return the frame at ``index`` as a ``(1, *frame_shape)`` view.

        The view aliases the buffer and is only valid until slot
        ``index % capacity`` is next written -- the consumer must copy or
        consume it before returning from its chunk handler, and must not retain
        it past telling the gate it is done with that index.
        """
        slot = index % self._capacity
        return self._buffer[slot : slot + 1]

    @property
    def capacity(self) -> int:
        """Number of slots."""
        return self._capacity

    @property
    def frame_shape(self) -> tuple[int, ...]:
        """Shape of one frame."""
        return self._frame_shape

    @property
    def dtype(self) -> np.dtype:
        """Frame dtype."""
        return self._dtype

    @property
    def nbytes(self) -> int:
        """Size of the frame store in bytes (for logging the allocation)."""
        return self._buffer.nbytes


class FrameGate:
    """Bounds how far the producer may run ahead of the consumer.

    The consumer reports how far it has got with :meth:`consumed_through`;
    the producer waits on :meth:`wait_until` for the frame whose slot it is
    about to reuse. **Which** frame that is, the producer decides -- it is
    the one it last wrote to that slot, which it alone knows.

    That indirection is not ceremony. Deriving the frame arithmetically as
    ``index - capacity`` assumes the index sequence has no holes, and a
    timelapse that skips a timepoint has exactly that: the source jumps the
    global index forward, and the producer ends up waiting on frames that
    are never going to be produced, let alone consumed.

    This replaces a per-stack handshake, where the producer stopped at every
    stack boundary until the whole previous stack had drained -- which is
    what made the buffer have to hold a whole stack.

    Counting is monotonic and never rewound, so a late or duplicated report
    cannot move the gate backwards and free a slot twice.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._consumed_through = -1
        self._released = False

    def consumed_through(self, index: int) -> None:
        """Report that every frame up to and including ``index`` is finished."""
        with self._condition:
            if index > self._consumed_through:
                self._consumed_through = int(index)
                self._condition.notify_all()

    def wait_until(self, index: int, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for ``index`` to be reported finished.

        Returns ``True`` once it has been (or the gate has been released),
        ``False`` on timeout -- so a caller can poll in slices and stay
        responsive to interruption, the way it would with an ``Event``.
        """
        with self._condition:
            if self._released or self._consumed_through >= index:
                return True
            self._condition.wait(timeout)
            return self._released or self._consumed_through >= index

    def reached(self, index: int) -> bool:
        """Whether every frame up to ``index`` has been reported finished."""
        with self._condition:
            return self._released or self._consumed_through >= index

    def release(self) -> None:
        """Let every waiter through, for shutdown."""
        with self._condition:
            self._released = True
            self._condition.notify_all()

    @property
    def released(self) -> bool:
        """Whether the gate has been released."""
        with self._condition:
            return self._released

    @property
    def position(self) -> int:
        """Highest frame index reported finished; ``-1`` before any."""
        with self._condition:
            return self._consumed_through


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

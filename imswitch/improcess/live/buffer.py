"""Stack-sized frame store shared between the live stream and process workers.

``StackRing`` is a pure, fixed, **stateless** structure: a preallocated
``(num_frames, *frame_shape)`` buffer and nothing else. It holds one stack's
worth of raw frames, addressed by **ring index**; slot ``i`` holds whichever
frame has ring index ``i (mod num_frames)``.

  * the stream worker calls :meth:`write` (one frame, by ring index) then
    emits that ring index on a Qt signal,
  * the process worker calls :meth:`read` (same ring index) to get the frame
    by reference and hand it to ``session.push``.

Everything stateful lives outside the ring:

  * the "consumer has drained stack N before the producer starts stack N+1"
    barrier is a ``threading.Event`` in the worker/controller layer,
  * deciding whether a given ring index may be written now (stack boundaries,
    validity) is the stream worker's job.

Because the ring keeps no epoch or tag, a :meth:`read` for a slot the producer
has already recycled silently returns the newer frame -- correctness rests
entirely on the barrier.
"""

import numpy as np


class RawDataBuffer:
    """Buffer used to store the retrieved raw-data from the LiveStreamWorker"""

    def __init__(self):

        ...


class StackRing:
    """One stack's worth of single-frame slots, addressed by ring index."""

    def __init__(
        self,
        num_frames: int,
        frame_shape: tuple[int, ...],
        dtype,
    ) -> None:
        """Allocate the frame store.

        Args:
            num_frames: Frames per stack -- also the number of slots.
            frame_shape: Shape of a single raw frame (typically ``(H, W)``).
            dtype: Frame dtype (anything ``np.dtype`` accepts).

        Raises:
            ValueError: ``num_frames < 1`` or ``frame_shape`` has a non-positive
                dim.
        """
        if num_frames < 1:
            raise ValueError("The expected number of frames to be stored is less than 1.")

        if any(d <= 0 for d in frame_shape):
            raise ValueError("A dimension in the frame shape is non-positive")

        self._num_frames = num_frames
        self._frame_shape = frame_shape
        self._dtype = np.dtype(dtype)
        self._mem_ring = np.empty((self._num_frames, *self._frame_shape), self._dtype)

    def write(self, index: int, frame: np.ndarray) -> None:
        """Copy one raw frame into slot ``index % num_frames``.

        Args:
            index: Ring index of the frame -- the ring maps it to a slot with
                ``% num_frames``.
            frame: Frame data, shape ``frame_shape``.
        """
        self._mem_ring[index % self._num_frames] = frame

    def read(self, index: int) -> np.ndarray:
        """Return the frame at ring ``index`` as a ``(1, *frame_shape)`` view.

        The view aliases the ring buffer and is only valid until slot
        ``index % num_frames`` is next written -- the consumer must copy or
        consume it before returning from its chunk handler, and must not retain
        it across the stack barrier.
        """
        slot = index % self._num_frames
        return self._mem_ring[slot : slot + 1]

    @property
    def num_frames(self) -> int:
        """Number of slots (== frames per stack)."""
        return self._num_frames

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
        return self._mem_ring.nbytes


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

"""Bounded, ordered raw-frame handoff, independent of Qt and disk format.

A reader owns these arrays until a processing worker acknowledges an item.
No data are silently skipped, duplicated or substituted for missing timepoints.
Physical readers remain responsible for detecting/filling explicit gaps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FrameBlock:
    start: int
    end: int
    data: np.ndarray
    # A sliced remainder still owns its original parent allocation. This is
    # the actual retained memory until the last frame has been consumed.
    allocation_bytes: int | None = None


class FrameInbox:
    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self._blocks: deque[FrameBlock] = deque()
        self._bytes = 0
        self._frame_count = 0
        self._next_input: int | None = None

    @property
    def nbytes(self) -> int:
        return self._bytes

    @property
    def available(self) -> int:
        return self._frame_count

    @property
    def next_input(self) -> int | None:
        return self._next_input

    @property
    def first_index(self) -> int | None:
        return self._blocks[0].start if self._blocks else None

    def append(self, chunks) -> None:
        """Accept a source's poll batch atomically, without exceeding budget."""
        prepared: list[FrameBlock] = []
        expected = self._next_input
        size = self._bytes
        for chunk in chunks:
            data = np.asarray(chunk.data)
            start, end = int(chunk.start), int(chunk.end)
            if data.ndim != 3 or end <= start or len(data) != end - start:
                raise ValueError(f"Invalid raw frame range [{start}:{end}] shape {data.shape}")
            if expected is not None and start != expected:
                raise ValueError(f"Non-contiguous raw frames: expected {expected}, got {start}")
            expected = end
            size += data.nbytes
            if size > self.max_bytes:
                raise MemoryError(
                    f"Live reader returned {size} buffered bytes; budget {self.max_bytes}. "
                    "Reduce source poll size or increase the configured budget."
                )
            prepared.append(FrameBlock(start, end, data, data.nbytes))
        self._blocks.extend(prepared)
        self._bytes = size
        self._frame_count += sum(b.end - b.start for b in prepared)
        if prepared:
            self._next_input = expected

    def take(self, frames: int) -> FrameBlock | None:
        """Consume exactly N raw frames or leave the inbox untouched."""
        if frames < 1:
            raise ValueError("Requested frames must be positive")
        if self.available < frames:
            return None
        start = self._blocks[0].start
        end = start + frames
        remaining = frames
        arrays: list[np.ndarray] = []
        while remaining:
            block = self._blocks.popleft()
            count = min(remaining, block.end - block.start)
            arrays.append(block.data[:count])
            if count < block.end - block.start:
                # Retain a view, not a copy of almost the entire block on
                # every single-frame push. Account for its backing allocation
                # until the last frame is consumed (avoids quadratic copying).
                self._blocks.appendleft(FrameBlock(
                    block.start + count, block.end, block.data[count:],
                    block.allocation_bytes,
                ))
            else:
                self._bytes -= block.allocation_bytes or block.data.nbytes
            self._frame_count -= count
            remaining -= count
        # Own the item delivered across threads. No references to source's
        # reused I/O buffers or the inbox's remaining views escape.
        data = np.concatenate(arrays) if len(arrays) > 1 else arrays[0].copy()
        return FrameBlock(start, end, data)

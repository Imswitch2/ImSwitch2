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
    data: np.ndarray | None
    # A sliced remainder still owns its original parent allocation. This is
    # the actual retained memory until the last frame has been consumed.
    allocation_bytes: int | None = None
    missing: bool = False


class FrameInbox:
    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self._blocks: deque[FrameBlock] = deque()
        self._bytes = 0
        self._frame_count = 0
        self._next_input: int | None = None
        self._frame_shape: tuple[int, ...] | None = None
        self._dtype: np.dtype | None = None
        self._missing_ranges: list[tuple[int, int]] = []

    @property
    def missing_ranges(self) -> tuple[tuple[int, int], ...]:
        """Exact raw-frame intervals explicitly skipped by the source."""
        return tuple(self._missing_ranges)

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

    def append(self, chunks, *, allowed_gaps=()) -> None:
        """Append one poll atomically. Only *source-declared* gaps are padded.

        Lapse readers declare gaps only after observing a later physical
        ``scanN``/timepoint; an absent future file is never considered missing.
        Placeholder blocks account for logical indices without allocating a
        complete blank stack. They become zero frames only when consumed.
        ``missing_ranges`` must be retained alongside results as provenance.
        """
        prepared: list[FrameBlock] = []
        expected = 0 if self._next_input is None else self._next_input
        size = self._bytes
        new_missing = []
        added_frames = 0
        frame_shape, dtype = self._frame_shape, self._dtype
        allowed = {(int(s), int(e)) for s, e in allowed_gaps}
        for chunk in chunks:
            data = np.asarray(chunk.data)
            start, end = int(chunk.start), int(chunk.end)
            if data.ndim != 3 or end <= start or len(data) != end - start:
                raise ValueError(f"Invalid raw frame range [{start}:{end}] shape {data.shape}")
            if frame_shape is not None and (data.shape[1:] != frame_shape or data.dtype != dtype):
                raise ValueError("Raw frame shape/dtype changed within one stream")
            frame_shape, dtype = data.shape[1:], data.dtype
            if start < expected:
                raise ValueError(f"Overlapping raw frames: expected {expected}, got {start}")
            if start > expected:
                if (expected, start) not in allowed:
                    raise ValueError(
                        f"Non-contiguous raw frames: Unannounced raw frame gap: expected {expected}, got {start}"
                    )
                prepared.append(FrameBlock(expected, start, None, 0, missing=True))
                new_missing.append((expected, start))
                added_frames += start - expected
            expected = end
            size += data.nbytes
            if size > self.max_bytes:
                raise MemoryError(
                    f"Live reader returned {size} buffered bytes; budget {self.max_bytes}. "
                    "Reduce source poll size or increase the configured budget."
                )
            prepared.append(FrameBlock(start, end, data, data.nbytes))
            added_frames += end - start
        self._blocks.extend(prepared)
        self._bytes = size
        self._frame_count += added_frames
        if prepared:
            self._next_input = expected
            self._frame_shape, self._dtype = frame_shape, dtype
            self._missing_ranges.extend(new_missing)

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
            if block.missing:
                arrays.append(np.zeros((count, *self._frame_shape), dtype=self._dtype))
            else:
                arrays.append(block.data[:count])
            if count < block.end - block.start:
                # A sliced array keeps its parent's allocation alive, while
                # a missing placeholder has no backing allocation at all.
                self._blocks.appendleft(FrameBlock(
                    block.start + count, block.end,
                    None if block.missing else block.data[count:],
                    block.allocation_bytes, block.missing,
                ))
            elif not block.missing:
                self._bytes -= block.allocation_bytes or block.data.nbytes
            self._frame_count -= count
            remaining -= count
        # Own the item delivered across threads. No references to source's
        # reused I/O buffers or the inbox's remaining views escape.
        data = np.concatenate(arrays) if len(arrays) > 1 else arrays[0].copy()
        return FrameBlock(start, end, data)

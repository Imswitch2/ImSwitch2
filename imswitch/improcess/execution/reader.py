"""Incremental source/contract state machine; no queue, observer or Qt widgets.

Driven by a worker timer and acknowledged after each submitted processing item.
The source may represent an already-finished recording or a growing recording;
*readiness to publish* is deliberately owned by the future Session queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import Contract, RawInfo, raw_info_from_stack
from .frame_inbox import FrameBlock, FrameInbox


@dataclass(frozen=True)
class StreamItem:
    kind: str  # begin, push, finish
    frames: FrameBlock | None = None


class IncrementalReader:
    """Read/push gate: strictly one in-flight processing message."""

    def __init__(
        self, source, reconstructor_class, params: dict,
        *, name: str, path: Any = None, max_buffer_bytes: int = 256 * 1024 * 1024,
    ):
        self.source = source
        self.reconstructor_class = reconstructor_class
        self.params = dict(params)
        self.name = name
        self.path = path
        self.inbox = FrameInbox(max_bytes=max_buffer_bytes)
        self.info: RawInfo | None = None
        self.stack_info = None
        self.contract: Contract | None = None
        self._started = False
        self._begun = False
        self._finished = False
        self._awaiting_ack = False
        self._pause_requested = False
        self._done = False
        self._sent_next: int | None = None

    def open(self):
        if self._started:
            raise RuntimeError("Source opened twice")
        self._started = True
        self.stack_info = self.source.open(self.path)
        self.info = raw_info_from_stack(self.stack_info, name=self.name,
                                        source_path=str(self.path) if self.path is not None else None)
        self.contract = self.reconstructor_class.contract(self.info, self.params)
        if not isinstance(self.contract, Contract):
            raise TypeError("Streaming contract() must return Contract")
        # Initialization of a full scan stack can be larger than the normal
        # prefetch budget. The budget must accommodate at least that stack.
        frame_bytes = int(self.info.dtype.itemsize)
        for size in self.info.frame_shape:
            frame_bytes *= int(size)
        # Current LiveSource.poll() implementations may return one read-ahead
        # batch of up to 64 MiB. Leave that headroom beyond a mandatory
        # first-stack initialization, so a full scan can arrive even when the
        # final poll crosses the init-frame boundary.
        init_bytes = self.contract.begin_frames * frame_bytes
        self.inbox.max_bytes = max(self.inbox.max_bytes, init_bytes + 64 * 1024 * 1024)
        return self.info, self.contract

    @property
    def waiting_for_ack(self) -> bool:
        return self._awaiting_ack

    @property
    def done(self) -> bool:
        return self._done

    def request_pause(self, paused: bool):
        self._pause_requested = bool(paused)

    def prefetch(self):
        if not self._started or self._done or self._finished:
            return
        if self.inbox.nbytes >= self.inbox.max_bytes:
            return
        # Existing LiveSource implementations cap a poll to 64 MiB. Accumulate
        # enough small poll batches for begin() or the next chunk, but avoid
        # speculative reads while processing is busy.  A stack may require
        # hundreds of small chunks for its metadata-only initialization.
        need = self.contract.begin_frames if not self._begun else self.contract.chunk_frames
        if self.inbox.available >= need or (self._awaiting_ack and self.inbox.available):
            return
        self.inbox.append(self.source.poll())

    def next_item(self) -> StreamItem | None:
        if self._awaiting_ack or self._done:
            return None
        if not self._started:
            raise RuntimeError("Call open() first")
        assert self.contract is not None
        if not self._begun:
            count = self.contract.begin_frames
            block = self.inbox.take(count)
            if block is None:
                if self.source.is_complete():
                    raise ValueError(f"Source ended before {count} initialization frames arrived")
                return None
            self._begun = True
            self._sent_next = block.end
            self._awaiting_ack = True
            return StreamItem("begin", block)
        if self._pause_requested and self._sent_next is not None:
            per_t = self.info.frames_per_timepoint
            if self._sent_next % per_t == 0:
                return None
        count = self.contract.chunk_frames
        block = self.inbox.take(count)
        if block is None and self.source.is_complete() and self.inbox.available:
            # The last partial chunk is not silently discarded.
            block = self.inbox.take(self.inbox.available)
        if block is not None:
            if block.start != self._sent_next:
                raise ValueError(f"Raw frame gap: expected {self._sent_next}, got {block.start}")
            self._sent_next = block.end
            self._awaiting_ack = True
            return StreamItem("push", block)
        if self.source.is_complete() and not self.inbox.available:
            self._finished = True
            self._awaiting_ack = True
            return StreamItem("finish")
        return None

    def acknowledge(self) -> None:
        if not self._awaiting_ack:
            raise RuntimeError("Unexpected process acknowledgement")
        self._awaiting_ack = False
        if self._finished:
            self._done = True

    def close(self) -> None:
        self._done = True
        self.source.close()

"""Bounded, thread-safe frame queue shared by the real and mock IC4 drivers.

This is the piece that fixes the original TIS bug. The legacy ``pyicic`` path
read a *single buffer that the driver overwrites in place*, so two polls between
two triggers saw the same bytes and two triggers between two polls lost a frame.
Retaining every delivered frame until it is drained is the whole fix; whether the
frames arrive by callback (IC4) or by polling an SDK queue is incidental.

Real and mock drivers share this class deliberately, so the mock-backed tests
exercise the same overflow and draining code that runs against hardware.
"""

from __future__ import annotations

import threading
from collections import deque


class FrameQueue:
    """Retains delivered frames until drained, dropping oldest when full.

    The IC4 ``QueueSinkListener`` runs on the SDK's stream thread while ImSwitch
    consumers drain from Qt/worker threads, so every access is locked.

    The bound mirrors ``DetectorManager.readChunk``'s per-consumer cap: a stalled
    consumer must not be able to grow this without limit. Overflow is warned
    about exactly once, because a genuinely stalled consumer would otherwise
    produce one log line per frame.
    """

    def __init__(self, maxlen: int = 256, logger=None):
        if maxlen < 1:
            raise ValueError(f"maxlen must be >= 1, got {maxlen}")
        self._maxlen = int(maxlen)
        self._frames: deque = deque()
        self._lock = threading.Lock()
        self._logger = logger
        self._dropped = 0
        self._warned = False
        # Most recent frame seen, retained independently of the drain queue —
        # see latest().
        self._last = None

    @property
    def maxlen(self) -> int:
        return self._maxlen

    @property
    def dropped(self) -> int:
        """Total frames discarded to overflow since construction."""
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    def push(self, frame) -> None:
        """Append one frame, dropping the oldest if already at capacity.

        The caller must pass a frame it owns outright. For IC4 that means
        ``ImageBuffer.numpy_copy()`` — never ``numpy_wrap()``, whose view dies
        with the buffer and would corrupt whatever is drained later.
        """
        with self._lock:
            if len(self._frames) >= self._maxlen:
                self._frames.popleft()
                self._dropped += 1
                if not self._warned:
                    self._warned = True
                    if self._logger is not None:
                        self._logger.warning(
                            f"Frame queue full ({self._maxlen}); dropping oldest "
                            f"frames. A consumer is not draining fast enough — "
                            f"further drops will not be logged."
                        )
            self._frames.append(frame)
            self._last = frame

    def drain(self) -> list:
        """Remove and return every queued frame, oldest first."""
        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
            return frames

    def latest(self):
        """Return the most recent frame seen, or None if there has been none.

        Deliberately *not* read off the pending deque. Live view polls this on a
        timer while a recording drains through getChunk, so reading the deque
        would return None for every poll that lands after a drain — the live
        view would flicker to black exactly while a recording is running.

        Survives drain() and is cleared only by clear(), which is what crop()
        calls: a retained frame with the pre-crop geometry would otherwise be
        handed out at the wrong shape.
        """
        with self._lock:
            return self._last

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._last = None

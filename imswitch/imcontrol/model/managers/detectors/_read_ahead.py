"""Driver reads of a point detector's finite input task, in chunks (scan
timing plan, S3).

The APD and PMT workers consume a scan line by line -- a line, a line
period, the samples to throw away -- and each consumption was one driver
read. At a 1 µs pixel a 512-pixel line is half a millisecond, so a scan
made some two thousand driver reads a second per detector. A
:class:`ReadAhead` fetches at least ``chunk`` samples per driver read and
hands them out in the sizes the worker asks for, in order, so the worker's
bookkeeping (what it has consumed, where a line starts) is unchanged.

``chunk`` is derived, not chosen: one live-update interval of samples
(``liveUpdateIntervalMs``, the detector's own setting): lines read faster
than the view redraws are not seen, and the recording takes the whole
frame. A fetch never runs past the samples the worker will consume
(:func:`raster_samples_consumed`), nor past the task's own sample count,
which a finite task never delivers beyond -- such a read would only wait
for its timeout. A request beyond either is fetched as asked.
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np


def read_ahead_samples(interval_s, rate_hz) -> int:
    """The samples one driver read should fetch at least: one live-update
    interval at the detector's sample rate; 0 (read what is asked) when
    either is not positive."""
    try:
        samples = float(interval_s) * float(rate_hz)
    except (TypeError, ValueError):
        return 0
    return max(0, int(samples))


def raster_samples_consumed(*, phase_delay, throw_startzero, scan_pads_initpos,
                            throw_init_smooth, loop_dims, samples_d_scanstep,
                            samples_d2_period) -> int:
    """The samples a raster scan worker (APD and PMT ``ScanWorker.run``)
    consumes in all, from its plan: the throws before the first line (the
    phase delay, the start padding, a higher axis's longer first move), then
    -- a 2-D scan -- the smooth start and every line's period but the last
    line's flyback; a scan of more dimensions realigns after every step of
    an outer axis to where the next step would begin, so it ends where its
    last outer step ends. Nothing after that is read: the last flyback, the
    final moves and the end padding, which also holds the phase delay
    (review 2026-10-16). A fetch past this point would wait on samples no
    line needs -- and, from a task one sample short, for its timeout."""
    pads = [int(p) for p in (scan_pads_initpos or [])]
    higher = max(pads[1:]) - pads[0] if len(pads) > 1 and max(pads[1:]) > pads[0] else 0
    start = int(phase_delay) + int(throw_startzero) + int(higher)
    dims = [int(d) for d in loop_dims]
    if len(dims) <= 2:
        lines = dims[1] if len(dims) > 1 else 1
        return (start + int(throw_init_smooth) + max(0, lines - 1) * int(samples_d2_period)
                + int(samples_d_scanstep[1]))
    outer = len(dims) - 1
    return start + dims[outer] * int(samples_d_scanstep[outer])


class ReadAhead:
    """The samples of one finite task, fetched ``chunk`` or more at a time
    by ``fetch(count, kind) -> array`` and handed out by :meth:`take`.
    ``total``: the samples the task delivers in all (``None``: unknown, so
    a fetch is never longer than what is asked)."""

    def __init__(self, fetch: Callable[[int, str], object], total: Optional[int], chunk: int):
        self._fetch = fetch
        self._total = None if total is None else int(total)
        self._chunk = max(0, int(chunk))
        self._fetched = 0
        self._buffer = None
        self._pos = 0
        #: How many driver reads were made: what the batching saves.
        self.fetches = 0

    @property
    def fetched(self) -> int:
        """Samples fetched from the source so far (consumed or not)."""
        return self._fetched

    def available(self) -> int:
        return 0 if self._buffer is None else len(self._buffer) - self._pos

    def take(self, count, kind='line'):
        """The next ``count`` samples, fetching as needed."""
        count = int(count)
        if count <= 0:
            return np.empty(0) if self._buffer is None else self._buffer[:0]
        while self.available() < count:
            need = count - self.available()
            want = max(need, self._chunk)
            if self._total is not None:
                left = self._total - self._fetched
                # never past the task's end; a request beyond it is fetched
                # as asked, and the driver says what is wrong
                want = need if left < need else min(want, left)
            data = np.asarray(self._fetch(want, kind))
            self.fetches += 1
            if data.size == 0:
                raise RuntimeError(f'The input task returned no samples for a read of {want}.')
            self._fetched += int(data.size)
            if self.available() == 0:
                self._buffer, self._pos = data, 0
            else:
                self._buffer = np.concatenate((self._buffer[self._pos:], data))
                self._pos = 0
        out = self._buffer[self._pos:self._pos + count]
        self._pos += count
        return out


__all__ = ['ReadAhead', 'raster_samples_consumed', 'read_ahead_samples']

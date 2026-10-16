"""Point detectors read their finite input task in chunks of one live-update
interval (scan timing plan, S3), handing the worker the sizes it asks for:
at a 1 µs pixel a line is half a millisecond, and one driver read per line
was some two thousand reads a second per detector."""
import importlib
import json

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._read_ahead import ReadAhead, read_ahead_samples

from .test_scan_clock_configurable import _FastRig


class _Source:
    """A finite task of ``total`` samples 0, 1, 2, ...; records each read."""

    def __init__(self, total):
        self.total, self.next, self.reads = total, 0, []

    def __call__(self, count, kind):
        assert self.next + count <= self.total, 'read past the end of a finite task'
        self.reads.append((count, kind))
        out = np.arange(self.next, self.next + count, dtype=np.int64)
        self.next += count
        return out


def test_the_chunk_is_one_live_update_interval_of_samples():
    assert read_ahead_samples(0.05, 1e6) == 50_000
    assert read_ahead_samples(0.05, 1e5) == 5_000
    assert read_ahead_samples(0.0, 1e6) == 0                 # 0 ms: read what is asked
    assert read_ahead_samples(None, 1e6) == 0


def test_reads_are_chunked_and_handed_out_in_order_never_past_the_end():
    source = _Source(total=100)
    reader = ReadAhead(source, total=100, chunk=40)
    taken = [reader.take(n, k) for n, k in ((5, 'throw'), (10, 'line'), (10, 'line'),
                                            (30, 'line'), (45, 'line'))]
    np.testing.assert_array_equal(np.concatenate(taken), np.arange(100))
    assert [len(t) for t in taken] == [5, 10, 10, 30, 45]
    assert source.reads == [(40, 'throw'), (40, 'line'), (20, 'line')]   # 100 in all, no more
    assert reader.fetches == 3 and reader.fetched == 100


def test_without_a_chunk_every_take_is_one_read_as_before():
    source = _Source(total=30)
    reader = ReadAhead(source, total=30, chunk=0)
    for n in (5, 10, 15):
        reader.take(n)
    assert [count for count, _ in source.reads] == [5, 10, 15]


def test_a_take_beyond_the_task_is_fetched_as_asked_and_an_empty_read_is_an_error():
    source = _Source(total=10)
    reader = ReadAhead(source, total=10, chunk=50)
    reader.take(4)                                       # fetches all 10
    with pytest.raises(AssertionError, match='past the end'):
        reader.take(12)                                  # 6 buffered, 6 more than the task has
    empty = ReadAhead(lambda count, kind: np.empty(0), total=None, chunk=0)
    with pytest.raises(RuntimeError, match='no samples'):
        empty.take(3)


def _scan(intervalMs, monkeypatch):
    """The 1 MHz galvo rig, seeded, its APD reading with ``intervalMs``;
    the image and the reads its workers made."""
    readers = []
    module = importlib.import_module('imswitch.imcontrol.model.managers.detectors.APDManager')
    base = module.ReadAhead

    class Counting(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            readers.append(self)

    monkeypatch.setattr(module, 'ReadAhead', Counting)
    rig = _FastRig(apdProps={'mockRandomSeed': 7, 'liveUpdateIntervalMs': intervalMs})
    try:
        rig.load(lengths=[2.0, 1.0, 0.0], steps=[0.02, 0.02, 1.0], dwell=1e-6)
        image = rig.run(timeoutS=60.0)
        lines = image.shape[-2]
    finally:
        rig.close()
    apd = [r for r in readers if r.fetched]
    return image, sum(r.fetches for r in apd), lines


def test_chunked_reads_give_the_same_image_with_far_fewer_driver_reads(qtbot, monkeypatch):
    per_line, per_line_reads, lines = _scan(0, monkeypatch)
    chunked, chunked_reads, _ = _scan(50, monkeypatch)
    np.testing.assert_array_equal(chunked, per_line)       # the same counts, pixel for pixel
    assert per_line_reads >= lines                          # a read per line, and the throws
    assert chunked_reads <= 3                               # the whole short scan: one interval

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


# --- the read limit: what the raster plan consumes, never the end padding -------------------

def _worker(scanInfoDict, signalDict, timerHz=1_000_000):
    """The real APD raster worker, on a simulated NI-DAQ, for one design."""
    from types import SimpleNamespace
    from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
    from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager, ScanWorker
    from .test_nidaq_clock_contract import _apd_info, _setup
    setup = _setup('Dev1/ctr2')
    nidaq = NidaqManager(setup)
    apd = APDManager(_apd_info(liveUpdateIntervalMs=5), 'APD', nidaq)
    worker = ScanWorker(apd, scanInfoDict, signalDict)
    worker.scanning = True
    return worker


def _galvo_3d(planes):
    from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner
    from .test_galvo_signal_goldens import _params, _setup_sted_like
    params = _params(['GalvoX', 'GalvoY', 'PiezoZ'], [2.0, 1.0, float(planes)], [0.5, 0.5, 1.0],
                     centers=[0.0, 0.0, 5.0])
    params['phase_delay'] = 30.0
    signals, _positions, info = GalvoScanDesigner().make_signal(params, _setup_sted_like())
    return info, {'scanSignalsDict': signals, 'TTLCycleSignalsDict': {}}


def _beta_3d(planes):
    from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner
    from .test_beta_signal_goldens import _params, _setup
    params = _params([20.0, 10.0, 2.0 * planes], [5.0, 5.0, 2.0], n_linesteps=2)
    signals, _positions, info = BetaScanDesigner().make_signal(params, _setup())
    return info, {'scanSignalsDict': signals, 'TTLCycleSignalsDict': {}}


@pytest.mark.parametrize('design', [lambda: _galvo_3d(1), lambda: _galvo_3d(3),
                                    lambda: _beta_3d(1), lambda: _beta_3d(3)],
                         ids=['galvo-2d', 'galvo-3planes', 'beta-1plane', 'beta-3planes'])
def test_the_reads_stop_where_the_raster_plan_ends(qtbot, design):
    """The worker consumes exactly what ``raster_samples_consumed`` says,
    and the read-ahead never fetches past it -- not the last flyback, the
    final moves or the end padding. A task one sample short (an input task
    that missed the timer's first edge) used to cost nothing, as the old
    line-by-line reads never reached its end; a chunk reaching it waited for
    the timeout and lost the frame (review 2026-10-16)."""
    info, signals = design()
    worker = _worker(info, signals)
    planned = worker._plannedSamples()
    worker.run()
    reader = worker.__dict__['_readAhead']
    assert worker._samples_read == planned
    assert reader.fetched <= planned <= worker._samples_total


def test_a_task_one_sample_short_still_completes_the_frame(qtbot):
    info, signals = _galvo_3d(1)
    worker = _worker(info, signals)
    short = worker._samples_total - 1
    original = worker._fetchSamples

    def fetch(count, kind):
        assert worker.__dict__['_readAhead'].fetched + count <= short, \
            'a read reached the sample the task never delivers'
        return original(count, kind)
    worker._fetchSamples = fetch
    worker.run()
    assert worker._samples_read == worker._plannedSamples() <= short

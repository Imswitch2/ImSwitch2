"""Tests for DetectorManager.readChunk — multi-consumer chunk distribution.

getChunk() is a destructive read: when the RecordingManager and BeadRec both
poll the same detector (scan-once recording during a bead scan), each frame
went to whichever consumer drained first, so the saved file AND the
reconstruction were both incomplete (the Snouty "408 of 900 frames" bug).
readChunk drains the hardware once and distributes every frame to every
registered consumer.
"""

import logging
import re
from pathlib import Path
from threading import Lock

import numpy as np

from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    DetectorManager,
    MAX_QUEUED_CONSUMER_FRAMES,
)


ROOT = Path(__file__).resolve().parents[4]


class _FakeDetector(DetectorManager):
    """Concrete subclass; constructed without the heavy base __init__."""

    def __init__(self):  # noqa: D107 — deliberately not calling super()
        self._chunkConsumers = {}
        self._chunkConsumersWarned = set()
        self._chunkConsumersLock = Lock()
        self._DetectorManager__logger = logging.getLogger('test.FakeDetector')
        self._pendingFrames = []

    def feed(self, n):
        """Queue n distinct fake frames as the next hardware chunk."""
        start = len(self._pendingFrames)
        self._pendingFrames = [np.full((2, 2), start + i) for i in range(n)]

    # --- DetectorManager abstract API ---
    def getChunk(self):
        frames, self._pendingFrames = self._pendingFrames, []
        return frames

    def getLatestFrame(self):
        return np.zeros((2, 2))

    def flushBuffers(self):
        self._pendingFrames = []

    def crop(self, hpos, vpos, hsize, vsize):
        pass

    def startAcquisition(self):
        pass

    def stopAcquisition(self):
        pass

    @property
    def pixelSizeUm(self):
        return [1, 1, 1]


def test_single_consumer_receives_all_frames():
    det = _FakeDetector()
    det.feed(5)
    frames = det.readChunk('only')
    assert len(frames) == 5
    assert len(det.readChunk('only')) == 0  # drained


def test_two_consumers_each_receive_every_frame():
    det = _FakeDetector()

    # First consumer registers by polling once (empty)
    assert det.readChunk('recording') == []
    assert det.readChunk('beadrec') == []

    det.feed(4)
    recFrames = det.readChunk('recording')   # drains hardware, distributes
    beadFrames = det.readChunk('beadrec')    # gets its own full copy

    assert len(recFrames) == 4
    assert len(beadFrames) == 4
    for a, b in zip(recFrames, beadFrames):
        assert np.array_equal(a, b)


def test_interleaved_polling_loses_nothing():
    det = _FakeDetector()
    det.readChunk('recording')
    det.readChunk('beadrec')

    totalRec, totalBead = 0, 0
    for batch in (3, 2, 4):
        det.feed(batch)
        totalRec += len(det.readChunk('recording'))
        det.feed(1)
        totalBead += len(det.readChunk('beadrec'))
    totalRec += len(det.readChunk('recording'))
    totalBead += len(det.readChunk('beadrec'))

    assert totalRec == 3 + 2 + 4 + 3   # all 12 frames
    assert totalBead == 12


def test_release_stops_retention():
    det = _FakeDetector()
    det.readChunk('recording')
    det.readChunk('beadrec')
    det.releaseChunkConsumer('beadrec')

    det.feed(3)
    assert len(det.readChunk('recording')) == 3
    # beadrec re-registers fresh — the 3 frames drained above are gone
    assert det.readChunk('beadrec') == []


def test_release_unknown_consumer_is_safe():
    det = _FakeDetector()
    det.releaseChunkConsumer('never-registered')


def test_idle_consumer_queue_is_capped():
    det = _FakeDetector()
    det.readChunk('idle')  # registers, then never polls again

    fed = 0
    while fed <= MAX_QUEUED_CONSUMER_FRAMES + 10:
        det.feed(50)
        det.readChunk('active')
        fed += 50

    assert len(det._chunkConsumers['idle']) <= MAX_QUEUED_CONSUMER_FRAMES
    # active consumer was drained every time and is unaffected
    assert det._chunkConsumers['active'] == []


def test_no_production_code_calls_get_chunk_outside_detector_layer():
    """Enforcement audit: getChunk() is a destructive read reserved for the
    detector layer itself (readChunk drains it once and distributes).
    Production code elsewhere must use readChunk(consumerKey), otherwise it
    silently steals frames from concurrent consumers."""
    detectorsDir = ROOT / 'imswitch' / 'imcontrol' / 'model' / 'managers' / 'detectors'
    callPattern = re.compile(r'\.getChunk\(\)')

    offenders = []
    for path in (ROOT / 'imswitch').rglob('*.py'):
        if detectorsDir in path.parents:
            continue  # the detector layer defines/wraps getChunk
        if '_test' in path.parts or 'lantzdrivers_mock' in path.parts:
            continue
        for lineNumber, line in enumerate(
                path.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
            if callPattern.search(line) and not line.lstrip().startswith('#'):
                offenders.append(f'{path.relative_to(ROOT)}:{lineNumber}')

    assert offenders == [], (
        'Direct .getChunk() calls outside the detector layer steal frames '
        'from concurrent consumers — use readChunk(consumerKey) instead '
        '(see DetectorManager.readChunk). Offenders: ' + ', '.join(offenders)
    )


def test_workflow_facade_reads_through_chunk_distribution():
    """CamFacade.get_data must not steal frames from concurrent consumers,
    and must keep its ndarray-or-None return contract."""
    from imswitch.imcontrol.model.workflows.facade import CamFacade

    det = _FakeDetector()
    cam = CamFacade(det)
    cam.prepare_acquisition(4)          # registers fresh (releases stale queue)
    det.readChunk('recording')          # concurrent consumer registers

    assert cam.get_data() is None       # empty -> None (registers facade)

    det.feed(4)
    workflowData = cam.get_data()
    recFrames = det.readChunk('recording')

    assert isinstance(workflowData, np.ndarray)
    assert workflowData.shape == (4, 2, 2)
    assert len(recFrames) == 4          # recording still got every frame


def test_ndarray_chunks_are_distributed_per_frame():
    """Hardware managers may return an (N, h, w) ndarray instead of a list."""
    det = _FakeDetector()
    det.readChunk('a')
    det.readChunk('b')

    det.getChunk = lambda: np.arange(3 * 2 * 2).reshape(3, 2, 2)
    framesA = det.readChunk('a')
    det.getChunk = lambda: []
    framesB = det.readChunk('b')

    assert len(framesA) == 3
    assert len(framesB) == 3
    assert framesA[0].shape == (2, 2)
    # np.array(listOfFrames) reassembles the stack (RecordingWorker contract)
    assert np.array(framesB).shape == (3, 2, 2)

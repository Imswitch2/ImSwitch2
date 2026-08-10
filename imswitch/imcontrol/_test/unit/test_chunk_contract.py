"""The chunk contract: one drain, two representations, routed per consumer.

`getChunk()` is a destructive drain, so a detector that can offer more than one
form of its data has to produce them together. The half that matters is
asymmetric: a scan-driven detector publishes a display frame at every boundary
— once per Z plane — while its raw volume is only whole when the scan ends.
Publishing that volume early is the failure these cover, because a recording
asks for exactly one frame from a scan-driven detector and would keep the first
one it was given, planes unwritten and no error anywhere.
"""

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    MAX_QUEUED_RAW_FRAMES,
    ChunkKind,
    ChunkPayload,
)


class _FakeDetector:
    """The broker's chunk machinery, without any hardware under it.

    Mirrors what a scan-driven manager does: a display frame per boundary, and
    a raw volume latched until its scan reaches a terminal state.
    """

    def __init__(self, planes=3, channels=2, size=4):
        from threading import Lock

        self._chunkConsumers = {}
        self._chunkConsumerKinds = {}
        self._chunkConsumersWarned = set()
        self._chunkConsumersOverflowed = set()
        self._chunkConsumersLock = Lock()
        self._DetectorManager__logger = _Silent()
        self._DetectorManager__image = np.array([])
        self._DetectorManager__rawImage = None

        self._volume = np.zeros((channels, planes, size, size), np.uint16)
        self._planes = planes
        self._written = 0
        self._displayReady = False
        self._rawReady = False
        self._rawDelivered = True
        self._rawAborted = False
        self._rawFailedGenerations = set()

    # --- what a scan does to it -------------------------------------
    def writePlane(self):
        """One boundary: a plane lands and the display frame updates."""
        self._volume[:, self._written] = self._written + 1
        self._written += 1
        self._displayReady = True

    def finishScan(self, generation=1):
        if self._rawAborted or generation in self._rawFailedGenerations:
            return
        self._rawReady = True
        self._rawDelivered = False

    def abortScan(self, generation=1):
        self.markFailed(generation)

    def markFailed(self, generation=1):
        """An abort, or a worker whose run raised."""
        self._rawFailedGenerations.add(generation)
        self._rawReady = False
        self._rawDelivered = True

    # --- the contract ------------------------------------------------
    def getChunk(self):
        if not self._displayReady:
            return np.empty((0, 0, 0), np.uint16)
        self._displayReady = False
        return self._volume[:, self._written - 1].max(axis=0)[None, ...].copy()

    def drainChunk(self):
        display = self.getChunk()
        raw = np.empty((0, 0, 0), np.uint16)
        if self._rawReady and not self._rawDelivered:
            self._rawDelivered = True
            self._rawReady = False
            raw = np.expand_dims(np.array(self._volume, copy=True), axis=0)
        return ChunkPayload(display=display, raw=raw)

    # Borrowed wholesale from the real base class.
    readChunk = None
    startChunkConsumer = None
    releaseChunkConsumer = None
    _distributeChunkLocked = None
    _chunkKinds = None


class _Silent:
    def warning(self, *_a, **_k):
        pass


def _detector(**kwargs):
    """A fake wired to the real broker methods, so the contract is exercised."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    detector = _FakeDetector(**kwargs)
    for method in ('readChunk', 'startChunkConsumer', 'releaseChunkConsumer',
                   '_distributeChunkLocked', '_chunkKinds'):
        setattr(type(detector), method, getattr(DetectorManager, method))
    return detector


# ----------------------------------------------------------------------
# Acceptance criteria
# ----------------------------------------------------------------------


def test_raw_is_empty_at_every_intermediate_boundary():
    """1. A volume still being written is never handed out."""
    detector = _detector(planes=3)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    seen = []
    for _ in range(3):
        detector.writePlane()
        seen.append(detector.readChunk('rec'))

    assert [len(frames) for frames in seen] == [0, 0, 0]


def test_exactly_one_complete_volume_after_the_scan_ends():
    """2. And every plane in it is written."""
    detector = _detector(planes=3, channels=2)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    for _ in range(3):
        detector.writePlane()
    detector.finishScan()

    frames = detector.readChunk('rec')

    assert len(frames) == 1
    assert frames[0].shape == (2, 3, 4, 4)
    assert not np.any(frames[0] == 0), 'a plane was left unwritten'


def test_raw_and_display_consumers_each_get_their_own():
    """3. Concurrently, and neither starves the other."""
    detector = _detector(planes=2)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.startChunkConsumer('view', kind=ChunkKind.DISPLAY)

    detector.writePlane()
    viewFirst = detector.readChunk('view')
    recFirst = detector.readChunk('rec')

    detector.writePlane()
    detector.finishScan()
    viewSecond = detector.readChunk('view')
    recSecond = detector.readChunk('rec')

    assert len(viewFirst) == 1 and viewFirst[0].ndim == 2   # a plane
    assert len(recFirst) == 0                               # not yet whole
    assert len(viewSecond) == 1
    assert len(recSecond) == 1 and recSecond[0].ndim == 4    # the volume


def test_polling_raw_again_yields_nothing():
    """4. Never a second copy — a duplicate is as wrong as a partial."""
    detector = _detector()
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.writePlane()
    detector.finishScan()

    assert len(detector.readChunk('rec')) == 1
    assert len(detector.readChunk('rec')) == 0
    assert len(detector.readChunk('rec')) == 0


def test_an_aborted_scan_publishes_no_raw_frame():
    """6. A cancelled scan must not leak the volume it left behind."""
    detector = _detector(planes=3)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.writePlane()
    detector.abortScan()
    detector.finishScan()          # teardown reaches here on every path

    assert len(detector.readChunk('rec')) == 0


def test_a_scan_whose_worker_raised_publishes_no_raw_frame():
    """The worker signals completion either way, so the outcome must be kept.

    Without it a crashed run is indistinguishable from a finished one, and the
    volume it abandoned half-written would be advertised as whole.
    """
    detector = _detector(planes=3)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.writePlane()
    detector.markFailed(generation=1)
    detector.finishScan(generation=1)

    assert len(detector.readChunk('rec')) == 0


def test_a_later_generation_is_unaffected_by_an_earlier_failure():
    """Per generation, not a global flag: one bad scan must not silence the rest."""
    detector = _detector(planes=1)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.markFailed(generation=1)
    detector.finishScan(generation=1)
    assert len(detector.readChunk('rec')) == 0

    detector._written = 0
    detector.writePlane()
    detector.finishScan(generation=2)

    assert len(detector.readChunk('rec')) == 1


def test_a_raw_consumer_is_capped_far_lower_than_a_display_one():
    """A raw frame is a volume; the same frame count is not the same memory."""
    detector = _detector(planes=1)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    for _ in range(MAX_QUEUED_RAW_FRAMES + 4):
        detector._written = 0          # each iteration is a fresh scan
        detector.writePlane()
        detector.finishScan()
        detector._distributeChunkLocked(detector.drainChunk())

    with pytest.raises(Exception):
        detector.readChunk('rec')


def test_the_two_latest_frame_caches_do_not_overwrite_each_other():
    """5. And an empty drain still answers is_save=True correctly.

    This is the bug the whole contract exists to remove: the chunk branch used
    to ignore ``is_save`` entirely and cache one frame for everyone, so merely
    having a recording open silently downgraded every other reader on that
    detector — and vice versa.
    """
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    detector = _detector(planes=1, channels=2)
    type(detector).getLatestFrameShared = DetectorManager.getLatestFrameShared
    type(detector).getLatestFrame = lambda self, is_save=False: np.zeros((4, 4))
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    detector.writePlane()
    detector.finishScan()
    detector.getLatestFrameShared()                 # one drain fills both

    display = detector.getLatestFrameShared(is_save=False)
    raw = detector.getLatestFrameShared(is_save=True)

    assert display.ndim == 2, 'display reader got the raw volume'
    assert raw.ndim == 4, 'save reader got a display plane'


def test_a_consumer_that_asks_for_nothing_gets_display():
    """Every existing caller registered before kinds existed, and must not move."""
    detector = _detector()
    detector.startChunkConsumer('legacy')
    detector.writePlane()

    frames = detector.readChunk('legacy')

    assert len(frames) == 1
    assert frames[0].ndim == 2


def test_the_default_payload_serves_a_camera_unchanged():
    """A camera's chunk is its frames; both kinds are the same object."""
    frames = np.zeros((2, 4, 4), np.uint16)
    payload = ChunkPayload(display=frames, raw=frames)

    assert payload.of(ChunkKind.DISPLAY) is frames
    assert payload.of(ChunkKind.RAW) is frames

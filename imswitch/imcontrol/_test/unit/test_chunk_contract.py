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
    MAX_QUEUED_CONSUMER_BYTES,
    ChunkConsumerOverflowError,
    ChunkKind,
    ChunkPayload,
)


class _FakeDetector:
    """The broker's chunk machinery, without any hardware under it.

    Mirrors what a scan-driven manager does: a display frame per boundary, and
    a raw volume latched until its scan reaches a terminal state. It therefore
    declares ``rawFrameIsDeferred``, which is what selects the small raw queue
    cap -- see :class:`_CameraLikeDetector` for the other half of that contract.
    """

    #: One raw frame here is a whole volume, published once per completed scan.
    rawFrameIsDeferred = True

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


class _CameraLikeDetector:
    """The other detector family: one complete frame per exposure.

    Deliberately does NOT override ``drainChunk`` -- the base class default is
    what makes a camera's raw and display the same object, and that default is
    the reason a camera lands in the RAW branch of the queue cap at all. Modelling
    it any other way would test a detector that does not exist.
    """

    #: A camera frame is whole on arrival, so nothing about it is deferred.
    rawFrameIsDeferred = False

    name = 'FakeCamera'

    def __init__(self, size=4):
        from threading import Lock

        self._chunkConsumers = {}
        self._chunkConsumerKinds = {}
        self._chunkConsumersWarned = set()
        self._chunkConsumersOverflowed = set()
        self._chunkConsumersLock = Lock()
        self._DetectorManager__logger = _Silent()
        self._DetectorManager__image = np.array([])
        self._size = size
        self._pending = []
        self._exposures = 0

    def produceFrame(self):
        """One exposure completes and lands in the hardware chunk."""
        self._exposures += 1
        self._pending.append(
            np.full((self._size, self._size), self._exposures, np.uint16)
        )

    def getChunk(self):
        frames, self._pending = self._pending, []
        return np.array(frames) if frames else np.empty((0, 0, 0), np.uint16)

    # Borrowed wholesale from the real base class, drainChunk included.
    drainChunk = None
    readChunk = None
    startChunkConsumer = None
    releaseChunkConsumer = None
    _distributeChunkLocked = None
    _chunkKinds = None
    _chunkConsumerBytes = None
    _frameBytes = None


class _Silent:
    def warning(self, *_a, **_k):
        pass


#: Broker methods a double borrows from the real base class, so every test
#: exercises the shipped logic rather than a re-implementation of it.
_BROKER_METHODS = ('readChunk', 'startChunkConsumer', 'releaseChunkConsumer',
                   '_distributeChunkLocked', '_chunkKinds',
                   '_chunkConsumerBytes', '_frameBytes',
                   '_oversizedWarned', '_warnOversizedDelivery',
                   'getLatestFrameShared', '_latchIsUnconsumed',
                   '_markLatchConsumed')


def _wire(detector):
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    for method in _BROKER_METHODS:
        # Copy the descriptor, not what it resolves to: a staticmethod fetched
        # with getattr comes back as a plain function and would be re-bound as
        # an instance method on the double.
        setattr(type(detector), method, DetectorManager.__dict__[method])
    return detector


def _detector(**kwargs):
    """A fake wired to the real broker methods, so the contract is exercised."""
    return _wire(_FakeDetector(**kwargs))


def _cameraDetector(**kwargs):
    """A camera-shaped fake, wired the same way (and using real drainChunk)."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    detector = _wire(_CameraLikeDetector(**kwargs))
    type(detector).drainChunk = DetectorManager.drainChunk
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


def _framesWithinBudget(detector, frame) -> int:
    """How many of ``frame`` the queue budget holds for this detector."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    return MAX_QUEUED_CONSUMER_BYTES // DetectorManager._frameBytes(frame)


def test_the_queue_holds_what_fits_in_memory_not_a_fixed_frame_count():
    """The budget is bytes, so the frame count follows from the frame.

    Two frame counts used to be chosen by hand -- 1000 for a display consumer,
    16 for a raw one -- and a frame count bounds nothing: the same number is
    3 MB for a 76x20 crop and 8 GB for a full sensor. Worse, picking between
    them needed a per-detector category that every new detector had to
    remember to declare correctly, which is the same trap in a new place.
    """
    import numpy as np

    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    small = DetectorManager._frameBytes(np.zeros((20, 76), np.uint16))
    full = DetectorManager._frameBytes(np.zeros((2048, 2048), np.uint16))
    volume = DetectorManager._frameBytes(np.zeros((2, 100, 512, 512), np.uint16))

    # A fast camera on a small ROI -- the configuration that failed in the
    # field at 9338 fps -- gets tens of thousands of frames of slack.
    assert MAX_QUEUED_CONSUMER_BYTES // small > 10_000
    # A full sensor gets tens, because that is what the same memory buys.
    assert 8 < MAX_QUEUED_CONSUMER_BYTES // full < 200
    # And a point detector's assembled volume gets very few, which is the
    # bound the old raw cap was reaching for by proxy.
    assert MAX_QUEUED_CONSUMER_BYTES // volume < 8


def test_a_deferred_raw_consumer_is_bounded_by_the_same_memory():
    """A volume is large, so few of them fit; nothing declares that."""
    detector = _detector(planes=1)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.writePlane()
    detector.finishScan()
    detector._distributeChunkLocked(detector.drainChunk())
    fits = _framesWithinBudget(detector, detector.readChunk('rec')[0])

    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    for _ in range(fits + 4):
        detector._written = 0          # each iteration is a fresh scan
        detector.writePlane()
        detector.finishScan()
        detector._distributeChunkLocked(detector.drainChunk())

    with pytest.raises(ChunkConsumerOverflowError):
        detector.readChunk('rec')


def test_a_camera_raw_consumer_survives_a_stall_no_detector_had_to_declare():
    """The failure the field hit, and the category nobody has to remember.

    The cap was keyed on ``ChunkKind.RAW`` alone, and the recording worker
    registers RAW for *every* detector it records. A camera's raw frame is its
    display frame, one per exposure, so a camera recording got 16 frames of
    slack -- 1.7 ms at 9338 fps. Keying it on a per-detector flag fixed the
    number but kept the trap: a new detector that forgot to declare itself got
    the wrong budget. Bytes need no declaration.
    """
    detector = _cameraDetector()
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.produceFrame()
    detector._distributeChunkLocked(detector.drainChunk())
    fits = _framesWithinBudget(detector, detector.readChunk('rec')[0])
    assert fits > 1000, 'a small camera frame should buy a long stall'

    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    for _ in range(2000):
        detector.produceFrame()
        detector._distributeChunkLocked(detector.drainChunk())

    assert len(detector.readChunk('rec')) == 2000


def test_a_camera_raw_consumer_still_fails_once_the_memory_is_gone():
    """Bounding by bytes must not turn an unbounded leak into silence.

    A large frame is used deliberately: the budget it buys is small, which is
    the point of counting bytes, and it keeps this test from having to produce
    tens of thousands of frames to reach the same edge.
    """
    detector = _cameraDetector(size=1024)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.produceFrame()
    detector._distributeChunkLocked(detector.drainChunk())
    fits = _framesWithinBudget(detector, detector.readChunk('rec')[0])

    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    for _ in range(fits + 4):
        detector.produceFrame()
        detector._distributeChunkLocked(detector.drainChunk())

    with pytest.raises(ChunkConsumerOverflowError):
        detector.readChunk('rec')


def test_the_overflow_error_quotes_the_budget_that_actually_tripped():
    """The message used to hardcode one frame count whatever the kind.

    That is how a recording 17 frames behind reported "more than 1000 frames"
    in the only field log this failure ever produced.
    """
    detector = _detector(planes=1)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.writePlane()
    detector.finishScan()
    detector._distributeChunkLocked(detector.drainChunk())
    fits = _framesWithinBudget(detector, detector.readChunk('rec')[0])

    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    for _ in range(fits + 4):
        detector._written = 0
        detector.writePlane()
        detector.finishScan()
        detector._distributeChunkLocked(detector.drainChunk())

    with pytest.raises(ChunkConsumerOverflowError) as raised:
        detector.readChunk('rec')
    message = str(raised.value)
    assert str(MAX_QUEUED_CONSUMER_BYTES // (1024 * 1024)) in message
    assert 'MiB' in message


def test_the_recording_worker_registers_the_kind_this_budget_applies_to():
    """Bind the two files the bug lived between.

    ``test_chunk_contract`` never mentioned the RecordingManager and
    ``test_recording`` never mentioned ``ChunkKind``; both halves were correct
    on their own. This asserts the join.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import (
        _RECORDING_CHUNK_CONSUMER,
    )

    detector = _cameraDetector()
    detector.startChunkConsumer(_RECORDING_CHUNK_CONSUMER, kind=ChunkKind.RAW)

    for _ in range(100):
        detector.produceFrame()
        detector._distributeChunkLocked(detector.drainChunk())

    assert len(detector.readChunk(_RECORDING_CHUNK_CONSUMER)) == 100


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


def test_a_display_read_reuses_a_latch_another_consumer_just_refreshed():
    """The live view must not hand a stalled recorder its whole backlog.

    Every drain fans out to every registered consumer, so a live-view tick that
    drains 300 ms of a fast camera pushes all of it into a blocked recorder's
    queue at once. The frames are the recorder's to have -- but they can wait
    in the driver's buffer, where they are not counted against a budget the
    recorder cannot currently spend. When somebody else has already drained,
    the latch holds a frame the display has not seen and no drain is needed.
    """
    detector = _cameraDetector()
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    detector.produceFrame()
    detector.produceFrame()
    detector.readChunk('rec')          # the recorder drains: latch refreshed
    before = len(detector._chunkConsumers['rec'])

    detector.produceFrame()            # arrives in the driver, undrained
    frame = detector.getLatestFrameShared()

    assert frame is not None
    assert len(detector._chunkConsumers['rec']) == before, (
        'the display path drained and pushed frames into the recorder queue'
    )


def test_a_display_read_still_drains_when_nothing_else_does():
    """Otherwise a blocked recorder would freeze the live view with it."""
    detector = _cameraDetector()
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.produceFrame()
    detector.readChunk('rec')

    # First read takes the latch the recorder's drain left behind.
    detector.getLatestFrameShared()
    # The recorder is now blocked and never polls again; frames keep arriving.
    detector.produceFrame()
    detector.produceFrame()

    # The display must fetch them itself rather than showing a frozen image.
    detector.getLatestFrameShared()
    assert len(detector._chunkConsumers['rec']) > 0, (
        'the display path stopped draining, so nothing refreshes the view'
    )


def test_the_save_path_is_unaffected_by_latch_reuse():
    """A recording snap must still force a drain; it wants the newest data."""
    detector = _cameraDetector()
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.produceFrame()
    detector.readChunk('rec')
    detector.produceFrame()

    detector.getLatestFrameShared(is_save=True)

    assert len(detector._chunkConsumers['rec']) > 0


def test_an_overflowed_consumer_stops_costing_the_ones_still_reading():
    """Trimming a full queue on every drain is work inside a shared lock.

    Bounding the queue by bytes made it hold tens of thousands of small
    frames, and the trim popped them one at a time -- each pop moving the
    whole remaining queue -- on every subsequent drain, while holding the lock
    the recorder needs to read its own frames. Once a consumer is over budget
    its stream is incomplete however much is kept, and nothing retained for it
    can ever be returned, so it is released in one go and skipped after that.
    """
    detector = _cameraDetector(size=1024)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    detector.startChunkConsumer('idle', kind=ChunkKind.RAW)

    detector.produceFrame()
    detector._distributeChunkLocked(detector.drainChunk())
    fits = _framesWithinBudget(detector, detector.readChunk('rec')[0])

    for _ in range(fits + 4):
        detector.produceFrame()
        detector._distributeChunkLocked(detector.drainChunk())
        detector.readChunk('rec')          # the recorder keeps up

    # The idle consumer is over budget: nothing is retained for it any more.
    assert detector._chunkConsumers['idle'] == []
    assert detector._chunkConsumerBytes().get('idle', 0) == 0

    # Further drains cost it nothing, and the recorder is unaffected.
    detector.produceFrame()
    detector._distributeChunkLocked(detector.drainChunk())
    assert detector._chunkConsumers['idle'] == []
    assert len(detector.readChunk('rec')) == 1

    # And it still fails rather than accepting a gap.
    with pytest.raises(ChunkConsumerOverflowError):
        detector.readChunk('idle')

    # Restarting it is the documented recovery, and it works.
    detector.startChunkConsumer('idle', kind=ChunkKind.RAW)
    detector.produceFrame()
    detector._distributeChunkLocked(detector.drainChunk())
    assert len(detector.readChunk('idle')) == 1


# ----------------------------------------------------------------------
# One delivery to an empty queue is admitted whatever its size
# ----------------------------------------------------------------------
#
# The queue budget bounds a backlog. A scan-driven detector's raw frame is
# its whole assembled volume, which can exceed the budget on its own; the
# byte budget (862786bd) refused it -- dropped at publish, consumer blamed
# for "falling behind" -- where the frame count it replaced had admitted any
# single frame. The writer's enqueue_frames has always admitted a payload to
# an empty queue; the detector boundary now applies the same rule.


class _Recorder:
    def __init__(self):
        self.lines = []

    def warning(self, message, *_a, **_k):
        self.lines.append(str(message))

    def info(self, *_a, **_k):
        pass


def _shrinkBudget(monkeypatch, nbytes):
    from imswitch.imcontrol.model.managers.detectors import DetectorManager as module
    monkeypatch.setattr(module, 'MAX_QUEUED_CONSUMER_BYTES', int(nbytes))


def _scan(detector):
    """One complete scan: every plane written, then the terminal state."""
    detector._written = 0
    for _ in range(detector._planes):
        detector.writePlane()
    detector.finishScan()


def test_one_raw_volume_larger_than_the_budget_is_delivered_whole(monkeypatch):
    """The volume form: one frame, bigger than the whole queue budget."""
    detector = _detector(planes=4, channels=2, size=64)
    _shrinkBudget(monkeypatch, detector._volume.nbytes // 2)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    _scan(detector)
    frames = detector.readChunk('rec')

    assert len(frames) == 1
    assert frames[0].shape == detector._volume.shape
    np.testing.assert_array_equal(frames[0], detector._volume)
    assert 'rec' not in detector._chunkConsumersOverflowed


def test_one_burst_larger_than_the_budget_is_delivered_whole(monkeypatch):
    """The burst form: many frames in one delivery, each small, the sum not."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    camera = _cameraDetector(size=64)
    perFrame = DetectorManager._frameBytes(np.zeros((64, 64), np.uint16))
    _shrinkBudget(monkeypatch, 4 * perFrame)
    camera.startChunkConsumer('rec', kind=ChunkKind.RAW)

    for _ in range(10):
        camera.produceFrame()
    frames = camera.readChunk('rec')

    assert len(frames) == 10
    assert [int(frame[0, 0]) for frame in frames] == list(range(1, 11))
    assert 'rec' not in camera._chunkConsumersOverflowed


def test_a_delivery_behind_an_oversized_one_overflows_as_before(monkeypatch):
    """What the budget still bounds: anything queued behind such a delivery."""
    detector = _detector(planes=4, channels=2, size=64)
    _shrinkBudget(monkeypatch, detector._volume.nbytes // 2)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    _scan(detector)
    detector._distributeChunkLocked(detector.drainChunk())   # admitted alone
    _scan(detector)
    detector._distributeChunkLocked(detector.drainChunk())   # nothing fits behind it

    assert 'rec' in detector._chunkConsumersOverflowed
    with pytest.raises(ChunkConsumerOverflowError, match='stream is incomplete'):
        detector.readChunk('rec')


def test_deliveries_that_fit_are_admitted_after_an_oversized_one_is_read(monkeypatch):
    """Admitting one oversized delivery does not loosen the budget afterwards."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    camera = _cameraDetector(size=64)
    perFrame = DetectorManager._frameBytes(np.zeros((64, 64), np.uint16))
    _shrinkBudget(monkeypatch, 4 * perFrame)
    camera.startChunkConsumer('rec', kind=ChunkKind.RAW)

    for _ in range(10):
        camera.produceFrame()
    assert len(camera.readChunk('rec')) == 10          # oversized, alone

    for _ in range(2):
        camera.produceFrame()
    camera._distributeChunkLocked(camera.drainChunk())  # two held
    camera.produceFrame()
    assert len(camera.readChunk('rec')) == 3           # three fit in four

    for _ in range(3):
        camera.produceFrame()
    camera._distributeChunkLocked(camera.drainChunk())  # three held
    for _ in range(2):
        camera.produceFrame()
    camera._distributeChunkLocked(camera.drainChunk())  # five do not
    with pytest.raises(ChunkConsumerOverflowError):
        camera.readChunk('rec')


def test_an_oversized_delivery_is_said_once_and_names_the_setting(monkeypatch):
    """Not a fault, but the operator learns that no backlog fits behind it."""
    detector = _detector(planes=4, channels=2, size=64)
    _shrinkBudget(monkeypatch, detector._volume.nbytes // 2)
    log = _Recorder()
    detector._DetectorManager__logger = log
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)

    for _ in range(3):
        _scan(detector)
        detector.readChunk('rec')

    assert len(log.lines) == 1
    assert 'memory.perDetectorQueueMB in imcontrol_options.json' in log.lines[0]
    assert 'admitted' in log.lines[0]
    assert 'rec' not in detector._chunkConsumersOverflowed

    # A fresh registration is a fresh consumer, and is told again.
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    _scan(detector)
    detector.readChunk('rec')
    assert len(log.lines) == 2


def test_the_configured_per_queue_limit_is_the_one_the_queue_honours():
    """memory.perDetectorQueueMB moves the budget; the literal is only the default."""
    from types import SimpleNamespace

    from imswitch.imcommon.model import memory_limits
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorManager,
    )

    memory_limits.configure(SimpleNamespace(perDetectorQueueMB=1), logger=_Recorder())
    camera = _cameraDetector(size=64)                     # 8 KiB frames
    perFrame = DetectorManager._frameBytes(np.zeros((64, 64), np.uint16))
    fits = (1024 * 1024) // perFrame
    log = _Recorder()
    camera._DetectorManager__logger = log
    camera.startChunkConsumer('idle', kind=ChunkKind.RAW)

    for _ in range(fits + 2):
        camera.produceFrame()
        camera._distributeChunkLocked(camera.drainChunk())   # one frame per delivery

    assert 'idle' in camera._chunkConsumersOverflowed
    assert any('1.0 MiB queue budget' in line for line in log.lines)
    assert any('memory.perDetectorQueueMB' in line for line in log.lines)
    with pytest.raises(ChunkConsumerOverflowError, match='perDetectorQueueMB'):
        camera.readChunk('idle')

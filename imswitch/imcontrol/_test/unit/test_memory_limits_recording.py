"""The recording queues under stall: the four acquisition shapes.

``docs/design/plans/memory-budgets.md``, *Acceptance*: any change to queue
behaviour is gated on this suite. It drives actual arrival schedules against
the real broker (``DetectorManager``) and the real ``WriterThread`` with a
storer whose disk the test stalls at will, and asserts on per-queue
occupancy -- not on an average rate, which is exactly what hides bursts and
contention between consumers. In every shape: no frame is lost while the
backlog fits; a stall that does not fit fails predictably and loudly, naming
the detector, the limit and the setting; and one oversized delivery is
delivered intact at both boundaries.
"""
import importlib
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model import memory_limits
from imswitch.imcommon.model.memory_limits import MIB
from imswitch.imcontrol.model import SaveMode
from imswitch.imcontrol.model.managers.RecordingManager import (
    WriterThread, _memoryEstimateLine,
)

# The packages re-export the classes under the modules' own names, so a
# ``from ... import RecordingManager`` would hand back the class; the
# constants being patched live on the modules.
writer_module = importlib.import_module('imswitch.imcontrol.model.managers.RecordingManager')
detector_module = importlib.import_module(
    'imswitch.imcontrol.model.managers.detectors.DetectorManager'
)
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    ChunkConsumerOverflowError, ChunkKind, DetectorManager,
)

from .test_chunk_contract import _cameraDetector, _detector


# ----------------------------------------------------------------------
# Fixtures: a stallable disk, a writer, a log, a schedule
# ----------------------------------------------------------------------


class _GateStorer:
    """Every write waits at a gate the test holds shut to stage a disk stall."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.first_write = threading.Event()
        self.writes = []            # (detectorName, frames) in arrival order

    def openStream(self, **_kwargs):
        pass

    def writeFrames(self, detectorName, frames):
        self.first_write.set()
        assert self.gate.wait(timeout=30), 'the test never released the disk'
        self.writes.append((detectorName, np.asarray(frames)))

    def finalizeStream(self, *_args, **_kwargs):
        pass

    def streamPayloadInfo(self, *_args, **_kwargs):
        return None

    def abortStream(self, *_args, **_kwargs):
        pass

    def written(self, detectorName):
        """Every frame handed to the disk for one detector, in order."""
        frames = [np.asarray(batch) for name, batch in self.writes if name == detectorName]
        return np.concatenate(frames, axis=0) if frames else np.empty((0,))


class _Log:
    def __init__(self):
        self.lines = []

    def _take(self, message, *_a, **_k):
        self.lines.append(str(message))

    debug = info = warning = error = _take

    def first(self, needle):
        return next((i for i, line in enumerate(self.lines) if needle in line), None)


def _writer(storer, names=('CAM',), shape=(64, 64)):
    names = list(names)
    writer = WriterThread(
        storer=storer,
        fileDests={name: f'{name}.h5' for name in names},
        detectorNames=names,
        shapes={name: shape for name in names},
        attrs={name: {} for name in names},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={name: f'{name}.h5' for name in names},
        recordingManager=None,
    )
    writer.start()
    writer.wait_for_open()
    return writer


def _finish(writer, timeout=10.0):
    writer.finish()
    writer.join(timeout=timeout)
    assert not writer.is_alive(), 'the writer did not finish'


def _wait(predicate, timeout=5.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f'timed out waiting for {what}')


def _enqueue_in_background(writer, detectorName, frames):
    thread = threading.Thread(target=writer.enqueue_frames, args=(detectorName, frames))
    thread.daemon = True
    thread.start()
    return thread


def _scan(detector, tag):
    """One complete scan whose volume carries ``tag`` so scans can be told apart."""
    detector._written = 0
    for _ in range(detector._planes):
        detector.writePlane()
    detector._volume[0, 0, 0, 0] = tag
    detector.finishScan()


def _poll(camera, n):
    """``n`` exposures land, then the recording worker drains them."""
    for _ in range(n):
        camera.produceFrame()
    return np.stack(camera.readChunk('rec'))


def _arrive(camera, n):
    """``n`` exposures land while the worker is *not* draining (it is blocked)."""
    for _ in range(n):
        camera.produceFrame()
    camera._distributeChunkLocked(camera.drainChunk())


def _use_frames(monkeypatch, *, queueFrames, writerFrames, frameShape=(64, 64)):
    """Budgets stated in frames of ``frameShape`` uint16, so the numbers read."""
    perFrameQueued = DetectorManager._frameBytes(np.zeros(frameShape, np.uint16))
    frameBytes = int(np.prod(frameShape)) * 2
    monkeypatch.setattr(writer_module, 'WRITE_BATCH_FRAMES', 1)   # every item is written at once
    monkeypatch.setattr(detector_module, 'MAX_QUEUED_CONSUMER_BYTES', queueFrames * perFrameQueued)
    monkeypatch.setattr(writer_module, 'WRITER_QUEUE_MAX_BYTES', writerFrames * frameBytes)
    return perFrameQueued, frameBytes


@pytest.fixture
def log(monkeypatch):
    log = _Log()
    monkeypatch.setattr(writer_module, 'logger', log)
    return log


# ----------------------------------------------------------------------
# Shape 1: a large single volume, above both queue budgets
# ----------------------------------------------------------------------


def test_shape_1_a_volume_larger_than_both_queues_is_delivered_and_the_backlog_behind_it_is_bounded(monkeypatch, log):
    monkeypatch.setattr(writer_module, 'WRITE_BATCH_FRAMES', 1)
    detector = _detector(planes=4, channels=2, size=64)          # a 64 KiB volume
    detector._DetectorManager__logger = log
    volumeBytes = detector._volume.nbytes
    monkeypatch.setattr(detector_module, 'MAX_QUEUED_CONSUMER_BYTES', volumeBytes // 2)
    monkeypatch.setattr(writer_module, 'WRITER_QUEUE_MAX_BYTES', volumeBytes // 2)
    detector.startChunkConsumer('rec', kind=ChunkKind.RAW)
    storer = _GateStorer()
    storer.gate.clear()                                            # the disk is stalled throughout
    writer = _writer(storer, names=('APD',), shape=detector._volume.shape)

    def deliver(tag):
        _scan(detector, tag)
        return np.stack(detector.readChunk('rec'))                 # the worker drains one volume

    v1 = deliver(1)
    writer.enqueue_frames('APD', v1)                               # empty writer queue: admitted alone
    assert storer.first_write.wait(5)                              # and now stuck on the disk
    _wait(lambda: writer._queuedBytes == 0, what='v1 dequeued')
    v2 = deliver(2)
    writer.enqueue_frames('APD', v2)                               # queue empty again: admitted alone
    assert writer._queuedBytes == v2.nbytes
    v3 = deliver(3)
    producer = _enqueue_in_background(writer, 'APD', v3)           # v2 is waiting: v3 does not fit
    _wait(lambda: log.first('blocked enqueuing') is not None, what='the block line')
    blockLine = log.lines[log.first('blocked enqueuing')]
    assert 'memory.writerQueueMB in imcontrol_options.json' in blockLine
    assert writer._queuedBytes == v2.nbytes                        # v3 is in the worker's hands
    assert producer.is_alive()

    # Behind the blocked worker the detector queue takes exactly one volume.
    _scan(detector, 4)
    detector._distributeChunkLocked(detector.drainChunk())
    assert detector._chunkConsumerBytes()['rec'] == DetectorManager._frameBytes(detector._volume)
    assert 'rec' not in detector._chunkConsumersOverflowed
    _scan(detector, 5)
    detector._distributeChunkLocked(detector.drainChunk())        # a fifth finds every queue full
    assert 'rec' in detector._chunkConsumersOverflowed
    overflowLine = log.lines[log.first('has not read what was held')]
    assert 'memory.perDetectorQueueMB' in overflowLine
    assert log.first('blocked enqueuing') < log.first('has not read what was held')

    storer.gate.set()                                              # the disk comes back
    producer.join(5)
    assert not producer.is_alive()
    _finish(writer)
    written = storer.written('APD')
    assert written.shape == (3, *detector._volume.shape)
    assert [int(v[0, 0, 0, 0]) for v in written] == [1, 2, 3]     # intact, in order
    with pytest.raises(ChunkConsumerOverflowError, match='perDetectorQueueMB'):
        detector.readChunk('rec')


# ----------------------------------------------------------------------
# Shape 2: small rapid frames from a fast camera
# ----------------------------------------------------------------------


def _stalled_camera_recording(monkeypatch, log, *, queueFrames, writerFrames, perPoll):
    perFrameQueued, frameBytes = _use_frames(
        monkeypatch, queueFrames=queueFrames, writerFrames=writerFrames)
    camera = _cameraDetector(size=64)
    camera._DetectorManager__logger = log
    camera.startChunkConsumer('rec', kind=ChunkKind.RAW)
    storer = _GateStorer()
    storer.gate.clear()
    writer = _writer(storer)

    writer.enqueue_frames('CAM', _poll(camera, perPoll))           # written -> stuck on the disk
    assert storer.first_write.wait(5)
    _wait(lambda: writer._queuedBytes == 0, what='first item dequeued')
    for _ in range(writerFrames // perPoll):                       # fill the writer queue exactly
        writer.enqueue_frames('CAM', _poll(camera, perPoll))
    assert writer._queuedBytes == writerFrames * frameBytes
    producer = _enqueue_in_background(writer, 'CAM', _poll(camera, perPoll))  # this one blocks
    _wait(lambda: log.first('blocked enqueuing') is not None, what='the block line')
    return camera, storer, writer, producer, perFrameQueued


def test_shape_2_small_rapid_frames_survive_a_stall_the_backlog_can_hold(monkeypatch, log):
    camera, storer, writer, producer, perFrameQueued = _stalled_camera_recording(
        monkeypatch, log, queueFrames=20, writerFrames=30, perPoll=5)

    for _ in range(4):                                             # 20 arrive while blocked: they fit
        _arrive(camera, 5)
    assert camera._chunkConsumerBytes()['rec'] == 20 * perFrameQueued
    assert 'rec' not in camera._chunkConsumersOverflowed

    storer.gate.set()                                              # the stall ends in time
    producer.join(5)
    assert not producer.is_alive()
    writer.enqueue_frames('CAM', np.stack(camera.readChunk('rec')))   # the worker drains the 20
    _finish(writer)

    written = storer.written('CAM')
    assert [int(frame[0, 0]) for frame in written] == list(range(1, 5 + 30 + 5 + 20 + 1))
    assert log.first('resumed') is not None                         # and said when it resumed


def test_shape_2_small_rapid_frames_fail_loudly_when_the_stall_outlasts_the_backlog(monkeypatch, log):
    camera, storer, writer, producer, perFrameQueued = _stalled_camera_recording(
        monkeypatch, log, queueFrames=20, writerFrames=30, perPoll=5)

    for _ in range(4):
        _arrive(camera, 5)
    assert camera._chunkConsumerBytes()['rec'] == 20 * perFrameQueued
    _arrive(camera, 5)                                             # the 21st..25th do not fit

    assert 'rec' in camera._chunkConsumersOverflowed
    overflow = log.first('has not read what was held')
    assert overflow is not None
    assert 'memory.perDetectorQueueMB' in log.lines[overflow]
    assert log.first('blocked enqueuing') < overflow               # the writer was the cause: said first
    with pytest.raises(ChunkConsumerOverflowError, match='FakeCamera.*perDetectorQueueMB'):
        camera.readChunk('rec')

    storer.gate.set()
    producer.join(5)
    _finish(writer)
    written = storer.written('CAM')
    assert [int(frame[0, 0]) for frame in written] == list(range(1, 5 + 30 + 5 + 1))  # what was admitted


# ----------------------------------------------------------------------
# Shape 3: a burst after a late poll
# ----------------------------------------------------------------------


def test_shape_3_a_burst_after_a_late_poll_is_admitted_whole_at_both_boundaries(monkeypatch, log):
    _use_frames(monkeypatch, queueFrames=4, writerFrames=2)
    camera = _cameraDetector(size=64)
    camera._DetectorManager__logger = log
    camera.startChunkConsumer('rec', kind=ChunkKind.RAW)
    storer = _GateStorer()                                         # the disk is fine
    writer = _writer(storer)

    burst = _poll(camera, 10)                                      # one late poll: 10 frames, 4 fit
    assert burst.shape[0] == 10
    assert log.first('admitted, since nothing was waiting') is not None
    writer.enqueue_frames('CAM', burst)                            # 10 frames against a 2-frame writer budget
    _wait(lambda: writer._queuedBytes == 0, what='the burst dequeued')
    for _ in range(3):
        writer.enqueue_frames('CAM', _poll(camera, 1))             # ordinary polls keep flowing
        _wait(lambda: writer._queuedBytes == 0, what='a poll dequeued')
    _finish(writer)
    assert [int(frame[0, 0]) for frame in storer.written('CAM')] == list(range(1, 14))
    assert log.first('blocked enqueuing') is None                  # nothing waited once the burst was taken

    _arrive(camera, 2)                                             # two unread, then a burst behind them
    _arrive(camera, 10)
    assert 'rec' in camera._chunkConsumersOverflowed               # the budget still bounds the backlog


# ----------------------------------------------------------------------
# Shape 4: two detectors, a second consumer that stops polling
# ----------------------------------------------------------------------


def test_shape_4_an_idle_consumer_overflows_while_the_recording_never_blocks(monkeypatch, log):
    perFrameQueued, _ = _use_frames(monkeypatch, queueFrames=8, writerFrames=1000)
    camA, camB = _cameraDetector(size=64), _cameraDetector(size=64)
    camA._DetectorManager__logger = log
    for camera in (camA, camB):
        camera.startChunkConsumer('rec', kind=ChunkKind.RAW)
    camA.startChunkConsumer('bead', kind=ChunkKind.RAW)            # BeadRec registers, then never polls
    storer = _GateStorer()
    writer = _writer(storer, names=('A', 'B'))

    for _ in range(30):
        writer.enqueue_frames('A', _poll(camA, 1))
        writer.enqueue_frames('B', _poll(camB, 1))
    _finish(writer)

    for name in ('A', 'B'):
        assert [int(frame[0, 0]) for frame in storer.written(name)] == list(range(1, 31))
    assert 'bead' in camA._chunkConsumersOverflowed
    assert 'rec' not in camA._chunkConsumersOverflowed
    overflow = log.first('has not read what was held')
    assert overflow is not None and '"bead"' in log.lines[overflow]
    assert log.first('blocked enqueuing') is None                  # no writer block preceded it


# ----------------------------------------------------------------------
# The settings, at the writer; the estimate line
# ----------------------------------------------------------------------


def test_the_configured_writer_limit_is_the_one_the_writer_honours(monkeypatch, log):
    monkeypatch.setattr(writer_module, 'WRITE_BATCH_FRAMES', 1)
    memory_limits.configure(SimpleNamespace(writerQueueMB=1), logger=log)
    frame = np.zeros((1, 256, 128), np.uint16)                     # 64 KiB: 16 fill 1 MiB exactly
    storer = _GateStorer()
    storer.gate.clear()
    writer = _writer(storer, shape=(256, 128))

    writer.enqueue_frames('CAM', frame)
    assert storer.first_write.wait(5)
    _wait(lambda: writer._queuedBytes == 0, what='first item dequeued')
    for _ in range(16):
        writer.enqueue_frames('CAM', frame)
    assert writer._queuedBytes == MIB
    producer = _enqueue_in_background(writer, 'CAM', frame)
    _wait(lambda: log.first('blocked enqueuing') is not None, what='the block line')
    line = log.lines[log.first('blocked enqueuing')]
    assert 'of its 1 MiB' in line and 'memory.writerQueueMB' in line

    storer.gate.set()
    producer.join(5)
    _finish(writer)
    assert storer.written('CAM').shape[0] == 18


def test_the_estimate_line_names_both_settings_and_says_when_a_frame_does_not_fit():
    line = _memoryEstimateLine(
        ['CAM', 'APD'],
        {'CAM': (2048, 2048), 'APD': (130, 1024, 1024)},
        {'CAM': np.uint16, 'APD': np.uint16},
        {'CAM': False, 'APD': True},
        writerBudget=512 * MIB, queueBudget=256 * MIB,
    )
    assert 'an estimate, not a cap' in line
    assert 'memory.writerQueueMB' in line and 'memory.perDetectorQueueMB' in line
    assert 'CAM: 8.0 MiB per frame' in line and 'about 32 fit' in line
    assert 'write batch up to 32 frames = 256 MiB' in line
    assert 'APD: 260 MiB per frame (scan-driven' in line
    assert 'admitted alone, no further backlog fits behind it' in line

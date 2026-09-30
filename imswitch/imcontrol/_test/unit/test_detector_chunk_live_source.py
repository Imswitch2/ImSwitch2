"""DetectorChunkLiveSource: the detector chunk queue as an ImProcess LiveSource.

The doubles mirror the parts of ``DetectorManager`` the source touches --
``startChunkConsumer`` opens a "frames after now" boundary, ``readChunk``
drains the hardware into every registered consumer and returns the caller's
queue, an overflowed consumer raises on its next read -- so the tests pin the
contract the source relies on rather than the manager's internals.
"""

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_point_scan_layouts,
)
from imswitch.imcontrol.model.liverecon import (
    DetectorChunkLiveSource,
    build_live_stack_info,
    frame_shape_for_detector,
)
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    ChunkConsumerOverflowError,
    ChunkKind,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    RecordingPlan,
    build_recording_attrs,
)
from imswitch.improcess.live.workers import LiveStreamWorker
from imswitch.improcess.reconstructors.base import StackInfo


class _FakeDetector:
    """Hardware that queues frames, plus the chunk-consumer fan-out."""

    def __init__(self, name='CAM', shape=(5, 4)):
        self.name = name
        self.shape = shape                # (width, height), as DetectorManager
        self.dtype = np.dtype(np.uint16)
        self._hardware = []
        self.consumers = {}
        self.kinds = {}
        self.overflowed = set()
        self.calls = []

    def produce(self, *frames):
        self._hardware.extend(frames)

    def _distribute(self):
        frames, self._hardware = self._hardware, []
        for queue in self.consumers.values():
            queue.extend(frames)

    def startChunkConsumer(self, key, kind=ChunkKind.DISPLAY):
        self.calls.append(('start', key, kind))
        self._distribute()
        self.consumers[key] = []
        self.kinds[key] = kind
        self.overflowed.discard(key)

    def readChunk(self, key):
        self.calls.append(('read', key))
        self.consumers.setdefault(key, [])
        self._distribute()
        if key in self.overflowed:
            raise ChunkConsumerOverflowError(f'{self.name}: consumer {key!r} overflowed')
        frames = list(self.consumers[key])
        self.consumers[key].clear()
        return frames

    def releaseChunkConsumer(self, key):
        self.calls.append(('release', key))
        self.consumers.pop(key, None)
        self.kinds.pop(key, None)

    def overflow(self, key):
        self.consumers[key].clear()
        self.overflowed.add(key)


class _FakeDetectorsManager:
    def __init__(self, *detectors):
        self._detectors = {d.name: d for d in detectors}

    def execOn(self, name, func):
        return func(self._detectors[name])

    def __getitem__(self, name):
        return self._detectors[name]


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _frame(value, shape=(4, 5)):
    return np.full(shape, value, dtype=np.uint16)


def _info(frames_per_stack=4, expected_frames=None, frame_shape=(4, 5)):
    return StackInfo(
        frame_shape=frame_shape, dtype=np.dtype(np.uint16),
        frames_per_stack=frames_per_stack, expected_frames=expected_frames,
        detector_name='CAM',
    )


def _source(detector, info=None, **kwargs):
    manager = _FakeDetectorsManager(detector)
    return DetectorChunkLiveSource(manager, detector.name, info or _info(), **kwargs)


def test_arm_registers_a_raw_consumer_and_excludes_earlier_frames():
    detector = _FakeDetector()
    detector.produce(_frame(1), _frame(2))       # before the boundary
    source = _source(detector)

    source.arm()
    detector.produce(_frame(3), _frame(4), _frame(5))
    source.open()
    chunks = source.poll()

    assert detector.kinds['LiveRecon'] is ChunkKind.RAW
    assert len(chunks) == 1
    assert (chunks[0].start, chunks[0].end) == (0, 3)
    np.testing.assert_array_equal(chunks[0].data[:, 0, 0], [3, 4, 5])


def test_polls_carry_global_frame_indices():
    detector = _FakeDetector()
    source = _source(detector)
    source.open()

    detector.produce(_frame(1), _frame(2))
    first = source.poll()
    assert source.poll() == []
    detector.produce(_frame(3), _frame(4), _frame(5))
    second = source.poll()

    assert (first[0].start, first[0].end) == (0, 2)
    assert (second[0].start, second[0].end) == (2, 5)
    assert first[0].data.dtype == np.uint16
    assert source.stats.frames_received == 5


def test_open_arms_when_the_controller_did_not():
    detector = _FakeDetector()
    source = _source(detector)

    info = source.open()

    assert info is source.stack_info
    assert detector.calls[0] == ('start', 'LiveRecon', ChunkKind.RAW)


def test_poll_before_open_reads_nothing():
    detector = _FakeDetector()
    detector.produce(_frame(1))
    source = _source(detector)
    assert source.poll() == []
    assert detector.calls == []


def test_frame_transform_is_applied_per_frame():
    detector = _FakeDetector()
    source = _source(detector, _info(frame_shape=(5, 4)),
                     frame_transform=lambda frame: frame.T)
    source.open()
    detector.produce(_frame(7))

    chunks = source.poll()

    assert chunks[0].data.shape == (1, 5, 4)


def test_unexpected_frame_shape_is_an_error_not_a_silent_write():
    detector = _FakeDetector()
    source = _source(detector, _info(frame_shape=(4, 5)))
    source.open()
    detector.produce(_frame(1, shape=(8, 8)))

    with pytest.raises(ValueError, match='frame shape'):
        source.poll()


def test_frames_beyond_the_plan_are_discarded_and_counted():
    detector = _FakeDetector()
    source = _source(detector, _info(frames_per_stack=4, expected_frames=4))
    source.open()
    detector.produce(*[_frame(i) for i in range(6)])

    chunks = source.poll()

    assert (chunks[0].start, chunks[0].end) == (0, 4)
    assert source.stats.frames_discarded == 2
    assert source.is_complete() is True

    detector.produce(_frame(9))
    assert source.poll() == []
    assert source.stats.frames_discarded == 3


def test_overflow_re_arms_and_reports_an_incomplete_stream():
    detector = _FakeDetector()
    source = _source(detector)
    source.open()
    detector.produce(_frame(1))
    assert source.poll()[0].end == 1

    detector.overflow('LiveRecon')
    assert source.poll() == []
    assert source.stats.overflow_events == 1
    assert source.stats.incomplete is True
    assert detector.calls.count(('start', 'LiveRecon', ChunkKind.RAW)) == 2

    detector.produce(_frame(2))
    chunks = source.poll()
    assert (chunks[0].start, chunks[0].end) == (1, 2)


def test_completion_waits_for_the_drain_grace_after_the_end_mark():
    clock = _Clock(10.0)
    detector = _FakeDetector()
    source = _source(detector, _info(expected_frames=None), clock=clock,
                     drain_grace_s=0.5)
    source.open()

    assert source.is_complete() is False
    source.mark_stream_ended()
    assert source.is_complete() is False
    clock.now = 10.4
    assert source.is_complete() is False
    clock.now = 10.6
    assert source.is_complete() is True


def test_a_late_frame_restarts_the_drain_grace():
    clock = _Clock(10.0)
    detector = _FakeDetector()
    source = _source(detector, _info(expected_frames=None), clock=clock,
                     drain_grace_s=0.5)
    source.open()
    source.mark_stream_ended()

    clock.now = 10.3
    detector.produce(_frame(1))
    assert source.poll()[0].end == 1
    clock.now = 10.7
    assert source.is_complete() is False
    clock.now = 10.9
    assert source.is_complete() is True
    assert source.stats.ended is True


def test_all_planned_frames_complete_the_stream_without_an_end_mark():
    detector = _FakeDetector()
    source = _source(detector, _info(frames_per_stack=2, expected_frames=2))
    source.open()
    detector.produce(_frame(1), _frame(2))
    source.poll()
    assert source.is_complete() is True


def test_close_releases_the_consumer_once():
    detector = _FakeDetector()
    source = _source(detector)
    source.open()

    source.close()
    source.close()

    assert detector.calls.count(('release', 'LiveRecon')) == 1
    assert 'LiveRecon' not in detector.consumers
    detector.produce(_frame(1))
    assert source.poll() == []


def test_stats_flag_a_short_stream_only_once_it_has_ended():
    detector = _FakeDetector()
    source = _source(detector, _info(frames_per_stack=10, expected_frames=10))
    source.open()
    detector.produce(*[_frame(i) for i in range(4)])
    source.poll()

    assert source.stats.incomplete is False
    source.mark_stream_ended()
    assert source.stats.incomplete is True
    assert source.stats.frames_received == 4


def test_the_improcess_stream_worker_runs_over_the_source():
    """The real LiveStreamWorker startup and poll loop consume the source:
    first stack collected for begin(), the remainder streamed as chunks,
    completion from the planned frame count."""
    detector = _FakeDetector()
    source = _source(detector, _info(frames_per_stack=4, expected_frames=6))
    # The controller arms the consumer before the scan produces anything;
    # frames from before the boundary are, by contract, not this consumer's.
    source.arm()
    detector.produce(*[_frame(i) for i in range(6)])
    worker = LiveStreamWorker(source, do_open=True, poll_interval_ms=1,
                              open_max_attempts=3)

    opened, init_data, chunks, complete = [], [], [], []
    worker.sigOpened.connect(lambda info: opened.append(info))
    worker.sigInitStackReady.connect(lambda data: (init_data.append(data), worker.resume()))
    worker.sigChunkReady.connect(lambda chunk: chunks.append(chunk))
    worker.sigStackComplete.connect(lambda: complete.append(True))

    worker.run()

    assert opened[0].frames_per_stack == 4
    assert init_data[0].shape == (4, 4, 5)
    np.testing.assert_array_equal(init_data[0][:, 0, 0], [0, 1, 2, 3])
    streamed = np.concatenate([c.data for c in chunks], axis=0)
    np.testing.assert_array_equal(streamed[:, 0, 0], [4, 5])
    assert chunks[0].start == 4 and chunks[-1].end == 6
    assert complete == [True]
    assert source.is_complete() is True


class _OnePulseEach(dict):
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return 1

    def get(self, key, default=None):
        return 1


def test_build_live_stack_info_resolves_the_recorded_layout():
    layout = build_point_scan_layouts(
        {'img_dims': [3, 2], 'img_axes_phys': ['x', 'y'], 'pixel_sizes': [0.1, 0.2]},
        ('CAM',), scan_source='ScanControllerPointScan',
        pulse_counts=_OnePulseEach(),
    )['CAM']
    plan = RecordingPlan('ScanOnce', rec_frames=6, num_cam_ttl={'CAM': 1},
                         acquisition_layouts={'CAM': layout}, source_format='memory')
    attrs = build_recording_attrs(plan, 'CAM', {'ScanStage:target_device': 'X'},
                                  expected_frames=6)

    info = build_live_stack_info('CAM', (4, 5), np.uint16, attrs,
                                 frames_per_stack=6, num_timepoints=3)

    assert info.frame_shape == (4, 5)
    assert info.dtype == np.dtype(np.uint16)
    assert info.frames_per_stack == 6
    assert info.expected_frames == 18
    assert info.detector_name == 'CAM'
    assert info.dataset_path == '/CAM/data'
    assert info.attrs['recording:dataset_path'] == '/CAM/data'
    assert info.source_format == 'memory'
    assert info.acquisition_layout is not None
    assert info.acquisition_layout.is_usable
    assert info.acquisition_layout.layout.detector == 'CAM'
    assert info.attrs['ScanStage:target_device'] == 'X'


def test_build_live_stack_info_without_a_timepoint_count_leaves_the_end_open():
    plan = RecordingPlan('UntilStop', rec_frames=None)
    attrs = build_recording_attrs(plan, 'CAM', {})
    info = build_live_stack_info('CAM', (4, 5), 'uint8', attrs, frames_per_stack=16)

    assert info.expected_frames is None
    assert info.frames_per_stack == 16
    assert info.acquisition_layout is not None


def test_frame_shape_for_detector_reverses_width_height_and_measures_transforms():
    detector = _FakeDetector(shape=(640, 480))
    assert frame_shape_for_detector(detector) == (480, 640)
    assert frame_shape_for_detector(detector, lambda f: f.T) == (640, 480)

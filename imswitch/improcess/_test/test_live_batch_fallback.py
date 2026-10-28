"""The live runtime drives a batch Reconstructor one complete stack at a time.

``StackBatchSession`` is the adapter; ``LiveReconstructionController`` wraps
any reconstructor without ``supports_streaming`` in it instead of refusing,
and ``LiveProcessWorker`` treats a session with nothing to show yet as
exactly that.
"""

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.live import LiveSource, StackBatchSession
from imswitch.improcess.live.workers import LiveProcessWorker
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.reconstructors.base import (
    Chunk,
    Reconstructor,
    StackInfo,
    StreamInit,
)


class _Result(ProcessingResult):
    def save(self, path, fmt):
        return None


class _MeanRecon(Reconstructor):
    """Reduces a stack to one 2D image; records every call."""

    name = 'Mean'
    id = 'test-mean'

    def __init__(self):
        self.calls = []

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        frames = np.asarray(data_obj.data)
        self.calls.append({
            'frames': frames.copy(),
            'params': dict(params),
            'attrs': dict(data_obj.attrs),
            'name': data_obj.name,
            'dataset': data_obj.datasetName,
        })
        return _Result(data_obj.name, frames.mean(axis=0).astype(np.float32), ['Y', 'X'])


class _PassThroughRecon(_MeanRecon):
    name = 'Pass'
    id = 'test-pass'

    def process(self, data_obj, params: dict) -> ProcessingResult:
        self.calls.append({'frames': np.asarray(data_obj.data)})
        return _Result(data_obj.name, np.asarray(data_obj.data), ['T', 'Y', 'X'])


def _frames(start, count, shape=(3, 3)):
    return np.stack([np.full(shape, value, dtype=np.uint16) for value in range(start, start + count)])


def _init(data, fps=None, attrs=None, name='run'):
    info = StackInfo(frame_shape=data.shape[1:], dtype=data.dtype, frames_per_stack=fps,
                     detector_name='CAM', attrs=dict(attrs or {}))
    return StreamInit(name=name, dataset_name='CAM', data=data, attrs=dict(attrs or {}),
                      stack_info=info)


def test_the_first_stack_is_reconstructed_in_begin():
    recon = _MeanRecon()
    session = StackBatchSession(recon)

    plan = session.begin(_init(_frames(0, 4), fps=4, attrs={'ScanTTL:Nx': 2}),
                         {'roi': None})

    assert session.stacks_completed == 1
    assert plan.out_shape == (3, 3)
    assert plan.axis_labels == ['Y', 'X']
    call = recon.calls[0]
    np.testing.assert_array_equal(call['frames'], _frames(0, 4))
    assert call['params'] == {'roi': None}
    assert call['attrs']['ScanTTL:Nx'] == 2
    assert call['attrs']['recording:lapse_index'] == 0
    assert call['name'] == 'run'
    assert call['dataset'] == 'CAM'
    assert session.result() is not None
    assert session.result().name == 'run'


def test_result_object_is_stable_until_the_next_stack_completes():
    recon = _MeanRecon()
    session = StackBatchSession(recon)
    session.begin(_init(_frames(0, 4), fps=4), {})
    first = session.result()

    for index in range(4, 7):
        session.push(_frames(index, 1), index, index + 1)
        assert session.result() is first
        assert session.frames_in_current_stack == index - 3
    session.push(_frames(7, 1), 7, 8)

    second = session.result()
    assert second is not first
    assert session.stacks_completed == 2
    assert recon.calls[1]['attrs']['recording:lapse_index'] == 1
    np.testing.assert_array_equal(recon.calls[1]['frames'], _frames(4, 4))
    np.testing.assert_allclose(second.data, np.full((3, 3), 5.5))


def test_a_stack_left_with_holes_is_dropped_when_the_next_begins():
    recon = _MeanRecon()
    session = StackBatchSession(recon)
    session.begin(_init(_frames(0, 4), fps=4), {})

    session.push(_frames(4, 2), 4, 6)          # stack 1: two of four frames
    session.push(_frames(8, 1), 8, 9)          # stack 2 begins: stack 1 is abandoned
    assert session.stacks_dropped == 1
    assert session.stacks_completed == 1
    session.push(_frames(9, 3), 9, 12)

    assert session.stacks_completed == 2
    np.testing.assert_array_equal(recon.calls[1]['frames'], _frames(8, 4))
    assert recon.calls[1]['attrs']['recording:lapse_index'] == 2


def test_finish_drops_a_partial_final_stack_by_default():
    recon = _MeanRecon()
    session = StackBatchSession(recon)
    session.begin(_init(_frames(0, 4), fps=4), {})
    latest = session.result()
    session.push(_frames(4, 2), 4, 6)

    assert session.finish() is latest
    assert session.stacks_dropped == 1
    assert len(recon.calls) == 1


def test_finish_can_reconstruct_a_partial_final_stack_when_allowed():
    recon = _MeanRecon()
    session = StackBatchSession(recon, process_partial_final_stack=True)
    session.begin(_init(_frames(0, 4), fps=4), {})
    session.push(_frames(4, 2), 4, 6)

    final = session.finish()

    assert session.stacks_completed == 2
    assert recon.calls[1]['frames'].shape == (2, 3, 3)
    np.testing.assert_allclose(final.data, np.full((3, 3), 4.5))


def test_pass_through_results_do_not_alias_the_reused_buffer():
    recon = _PassThroughRecon()
    session = StackBatchSession(recon)
    session.begin(_init(_frames(0, 2), fps=2), {})
    first = session.result()

    session.push(_frames(2, 2), 2, 4)

    np.testing.assert_array_equal(first.data, _frames(0, 2))
    np.testing.assert_array_equal(session.result().data, _frames(2, 2))


def test_stack_size_comes_from_the_stack_info():
    recon = _MeanRecon()
    session = StackBatchSession(recon)
    session.begin(_init(_frames(0, 2), fps=2), {})
    assert session.frames_per_stack == 2
    session.push(_frames(2, 2), 2, 4)
    assert session.stacks_completed == 2


def test_a_short_first_stack_waits_for_the_rest():
    recon = _MeanRecon()
    session = StackBatchSession(recon)

    plan = session.begin(_init(_frames(0, 2), fps=4), {})

    assert session.result() is None
    assert plan.out_shape == (4, 3, 3)
    session.push(_frames(2, 2), 2, 4)
    assert session.result() is not None
    np.testing.assert_array_equal(recon.calls[0]['frames'], _frames(0, 4))


def test_chunk_length_must_match_its_index_range():
    session = StackBatchSession(_MeanRecon())
    session.begin(_init(_frames(0, 2), fps=2), {})
    with pytest.raises(ValueError, match='covers'):
        session.push(_frames(2, 2), 2, 5)


def test_push_before_begin_is_an_error():
    with pytest.raises(RuntimeError, match='begin'):
        StackBatchSession(_MeanRecon()).push(_frames(0, 1), 0, 1)


class _SnapshotSession:
    """result() answers from a script: None means nothing yet."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)

    def live_plane(self):
        return None

    def result(self):
        return self._snapshots.pop(0)


def test_worker_publishes_neither_empty_nor_unchanged_snapshots():
    first = _Result('a', np.zeros((2, 2)), ['Y', 'X'])
    second = _Result('b', np.ones((2, 2)), ['Y', 'X'])
    session = _SnapshotSession([None, None, first, first, second, second])
    worker = LiveProcessWorker(session, raw_buffer=None, frame_gate=None, frames_per_stack=4)
    published = []
    worker.sigResultUpdated.connect(lambda result: published.append(result))

    for _ in range(6):
        worker._publish_update()

    assert published == [first, second]


class _TestSource(LiveSource):
    def __init__(self, stack, chunk_size, frames_per_stack=None):
        self.stack = stack
        self.chunk_size = chunk_size
        self.frames_per_stack = frames_per_stack or stack.shape[0]
        self.cursor = 0
        self.name = 'test-source'

    def open(self, path_or_handle) -> StackInfo:
        return StackInfo(
            frame_shape=self.stack.shape[-2:], dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self.frames_per_stack, detector_name='CAM',
        )

    def poll(self):
        if self.cursor >= self.stack.shape[0]:
            return []
        start = self.cursor
        end = min(start + self.chunk_size, self.stack.shape[0])
        self.cursor = end
        return [Chunk(self.stack[start:end], start, end)]

    def is_complete(self) -> bool:
        return self.cursor >= self.stack.shape[0]


def _wait_for_finished(controller, timeout_ms=5000):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    loop = QtCore.QEventLoop()
    finished = []

    def on_finished():
        finished.append(True)
        loop.quit()

    controller.sigFinished.connect(on_finished)
    QtCore.QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    controller.sigFinished.disconnect(on_finished)
    return bool(finished)


def test_controller_drives_a_batch_reconstructor_stack_by_stack():
    """A reconstructor without supports_streaming is accepted, wrapped in a
    StackBatchSession, and yields one result per complete stack plus the
    final one through the live result path."""
    stack = np.arange(6 * 3 * 3, dtype=np.uint16).reshape(6, 3, 3)
    source = _TestSource(stack, chunk_size=2, frames_per_stack=3)
    recon = _PassThroughRecon()
    channel = CommunicationChannel()
    results = []
    channel.sigLiveResultUpdated.connect(lambda result: results.append(result))
    controller = LiveReconstructionController(channel)

    assert controller.start(recon, source, {}, source_arg='memory', name='batch-run') is True
    assert isinstance(controller._session, StackBatchSession)
    assert _wait_for_finished(controller)

    assert len(recon.calls) == 2
    np.testing.assert_array_equal(recon.calls[0]['frames'], stack[:3])
    np.testing.assert_array_equal(recon.calls[1]['frames'], stack[3:])
    assert results, 'no live result reached the channel'
    np.testing.assert_array_equal(results[-1].data, stack[3:])
    assert results[-1].name == 'batch-run'
    distinct = {id(result) for result in results}
    assert len(distinct) == 2
    assert controller.is_running is False

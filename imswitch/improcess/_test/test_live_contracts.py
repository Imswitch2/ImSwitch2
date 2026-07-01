from pathlib import Path

import numpy as np

from imswitch.improcess.live import InMemoryStackWrapper, LiveSource
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    Chunk,
    StackInfo,
    StreamInit,
    StreamPlan,
    StreamingReconstructor,
    StreamingSession,
)


class _Result(ProcessingResult):
    def save(self, path: Path, fmt: str) -> None:
        return None


class _Session(StreamingSession):
    def __init__(self):
        self.buffer = None
        self.closed = False

    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        frames = init_obj.stack_info.expected_frames if init_obj.stack_info else init_obj.data.shape[0]
        self.buffer = np.zeros((frames, *init_obj.data.shape[-2:]), dtype=np.float32)
        self.push(init_obj.data, 0, init_obj.data.shape[0])
        return StreamPlan(
            out_shape=self.buffer.shape,
            axis_labels=["T", "Y", "X"],
            view_modes=[ViewMode("Standard", (0, 1, 2))],
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        self.buffer[start:end] = chunk

    def result(self) -> ProcessingResult:
        return _Result("stream", self.buffer.copy(), ["T", "Y", "X"])

    def close(self) -> None:
        self.closed = True


class _Reconstructor(StreamingReconstructor):
    name = "Streaming test"
    id = "streaming-test"

    def make_param_widget(self, parent):
        raise NotImplementedError

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        return _Result("batch", np.asarray(data_obj.data), ["T", "Y", "X"])

    def make_session(self) -> StreamingSession:
        return _Session()


class _Source(LiveSource):
    def __init__(self, stack: np.ndarray, chunk_size: int):
        self.stack = stack
        self.chunk_size = chunk_size
        self.cursor = 0

    def open(self, path_or_handle) -> StackInfo:
        return StackInfo(
            frame_shape=self.stack.shape[-2:],
            dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self.stack.shape[0],
            attrs={"source": str(path_or_handle)},
        )

    def poll(self) -> list[Chunk]:
        if self.cursor >= self.stack.shape[0]:
            return []
        start = self.cursor
        end = min(start + self.chunk_size, self.stack.shape[0])
        self.cursor = end
        return [Chunk(self.stack[start:end], start, end)]

    def is_complete(self) -> bool:
        return self.cursor >= self.stack.shape[0]


def test_streaming_contract_round_trip():
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _Source(stack, chunk_size=2)
    info = source.open("memory")
    session = _Reconstructor().make_session()

    first = source.poll()[0]
    plan = session.begin(
        StreamInit(
            name="stack",
            dataset_name="CAM",
            data=first.data,
            attrs=info.attrs,
            stack_info=info,
        ),
        params={},
    )
    assert plan.out_shape == stack.shape

    while not source.is_complete():
        for chunk in source.poll():
            session.push(chunk.data, chunk.start, chunk.end)

    result = session.finish()
    np.testing.assert_array_equal(result.data, stack)
    session.close()
    assert session.closed is True


def test_in_memory_stack_wrapper_supports_batch_fallback():
    stack = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    wrapper = InMemoryStackWrapper(
        name="ram-stack",
        dataset_name="CAM",
        data=stack,
        attrs={"recording:expected_frames": 3},
    )

    assert wrapper.datasetName == "CAM"
    assert wrapper.dataLoaded is True
    assert wrapper.dataPath is None
    assert wrapper.numFrames == 3
    assert wrapper.attrs["recording:expected_frames"] == 3
    np.testing.assert_array_equal(wrapper.data, stack)
    np.testing.assert_array_equal(wrapper.getMeanData(), np.mean(stack, axis=0).astype(np.float32))

    result = _Reconstructor().process(wrapper, {})
    np.testing.assert_array_equal(result.data, stack)


def test_in_memory_stack_wrapper_exposes_dataobj_metadata_contract():
    # The live batch-fallback path wraps the buffered stack in
    # InMemoryStackWrapper and calls reconstructor.process() on it. Pass-through
    # reconstructors (View-only) read axis_labels/axis_scales/scale_unit off the
    # data object, so the wrapper must expose the same contract as DataObj or
    # process() raises AttributeError and the live result is silently dropped.
    stack = np.zeros((3, 4, 5), dtype=np.uint16)

    plain = InMemoryStackWrapper("ram", "CAM", stack, attrs={})
    assert plain.axis_labels == ["C", "Y", "X"]
    assert plain.axis_scales == [1.0, 1.0, 1.0]
    assert plain.scale_unit == "px"
    assert plain.source_info["dataset_name"] == "CAM"

    calibrated = InMemoryStackWrapper(
        "ram", "CAM", stack, attrs={"element_size_um": [1.0, 0.1, 0.1]}
    )
    assert calibrated.axis_scales == [1.0, 0.1, 0.1]
    assert calibrated.scale_unit == "um"


def test_view_only_reconstructs_from_in_memory_wrapper():
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    stack = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    wrapper = InMemoryStackWrapper(
        "ram", "CAM", stack, attrs={"element_size_um": [1.0, 0.2, 0.2]}
    )

    result = ViewOnlyReconstructor().process(wrapper, {})
    np.testing.assert_array_equal(result.data, stack)
    assert result.axis_scales == [1.0, 0.2, 0.2]
    assert result.scale_unit == "um"


def test_live_source_contract_yields_chunk_ranges():
    stack = np.zeros((5, 2, 3), dtype=np.float32)
    source = _Source(stack, chunk_size=2)
    info = source.open("memory")

    assert info.expected_frames == 5
    assert source.poll()[0].end == 2
    assert source.poll()[0].start == 2
    assert source.poll()[0].end == 5
    assert source.poll() == []
    assert source.is_complete() is True

"""Every path that produces a reconstruction records where it came from.

The plan's per-path exit test: one case per row of the "execution paths"
table. A path that stops going through the run envelope fails here, not in
a replay months later.
"""

import os
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.reconstructors.base import (  # noqa: E402
    Chunk,
    Reconstructor,
    StackInfo,
    StreamInit,
    StreamPlan,
    StreamingSession,
    ViewMode,
)
from imswitch.improcess.reconstructors.run import (  # noqa: E402
    run_consolidation,
    run_reconstruction,
)


# -- fixtures --------------------------------------------------------------------

class _Recon(Reconstructor):
    name = "Stub"
    id = "t.stub"
    supports_consolidation = True

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        data = np.asarray(data_obj.data)
        return ArrayProcessingResult(f"{data_obj.name} rec", data.astype(np.float32), ["T", "Y", "X"][-data.ndim:])

    def consolidate(self, results):
        return ArrayProcessingResult("merged", np.stack([r.data for r in results]), ["D", "T", "Y", "X"])


class _FileDataObj:
    def __init__(self, path, name="scan"):
        self.name = name
        self.dataPath = str(path)
        self.datasetName = "data"
        self.attrs = {"a": 1}
        self.data_handle = np.zeros((3, 4, 4), dtype=np.uint16)
        self.data = self.data_handle
        self.dataLoaded = True
        self.numFrames = 3

    def checkAndLoadData(self):
        pass


def _file(tmp_path, name="scan"):
    raw = tmp_path / f"{name}.h5"
    raw.write_bytes(b"0" * 8)
    return _FileDataObj(raw, name)


def _recon_node(result):
    node = output_node(result)
    assert node is not None, "no provenance on the result"
    assert node["op"] == "reconstruct", node["op"]
    return node


# -- 1. inline ---------------------------------------------------------------------

def test_inline_reconstruct_path_records(tmp_path):
    from unittest.mock import MagicMock

    from imswitch.improcess.controller.ReconstructorManagerController import (
        ReconstructorManagerController,
    )

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {"gain": 2}
    recon = _Recon()
    controller._main = SimpleNamespace(_activeReconstructor=recon, monalisaController=None)
    published = []
    controller._publishPluginResult = lambda result, name, reconstructor=None: published.append(result)

    controller._reconstruct_with_plugin([_file(tmp_path)], consolidate=False)

    node = _recon_node(published[0])
    assert node["plugin_id"] == "t.stub" and node["params"] == {"gain": 2}
    assert node["replayable"] is True


# -- 2./3. worker + consolidation ---------------------------------------------------

def test_worker_path_records_each_run_and_the_consolidation(tmp_path):
    from imswitch.improcess.controller.reconstruction_worker import (
        ReconstructionWorker,
        ReconstructionWorkerJob,
    )

    jobs = [
        ReconstructionWorkerJob(_file(tmp_path, "a"), {"k": 1}, None),
        ReconstructionWorkerJob(_file(tmp_path, "b"), {"k": 2}, None),
    ]
    outcomes = []
    worker = ReconstructionWorker(_Recon(), jobs, consolidate=True)
    worker.finished.connect(outcomes.append)
    worker.run()

    outcome = outcomes[0]
    assert len(outcome.runs) == 2
    assert [run.params for run in outcome.runs] == [{"k": 1}, {"k": 2}]
    for result in outcome.results:
        assert _recon_node(result)["replayable"] is True
    merged = output_node(outcome.merged)
    assert merged["op"] == "consolidate"
    assert merged["inputs"] == [run.node for run in outcome.runs]
    assert len(graph_of(outcome.merged)["nodes"]) == 5     # 2 sources + 2 recon + consolidate


# -- 4. legacy MoNaLISA -------------------------------------------------------------

def test_legacy_monalisa_adapter_records_with_its_settings(tmp_path):
    from imswitch.improcess.reconstructors.monalisa.legacy import LegacyMonalisaReconstructor

    class _Extractor:
        def extractSignal(self, data, sigmas, pattern, device):
            frames = data.shape[0]
            return np.ones((2, frames, 3, 3), dtype=np.float32)

    adapter = LegacyMonalisaReconstructor()
    adapter._signal_extractor = _Extractor()
    data_obj = _file(tmp_path)
    data_obj.data = np.ones((4, 6, 6), dtype=np.float32)
    data_obj.numFrames = 4
    from imswitch.improcess.reconstructors.monalisa.scan_params import DEFAULT_LABELS

    params = {
        **LegacyMonalisaReconstructor.default_params(),
        "pattern": [1.0, 2.0, 3.0, 4.0],
        "bleaching_correction": True,
        "scan_params": {
            "dimensions": [DEFAULT_LABELS.r_l, DEFAULT_LABELS.u_d, DEFAULT_LABELS.b_f, DEFAULT_LABELS.timepoints],
            "directions": [DEFAULT_LABELS.p] * 3,
            "steps": ["2", "2", "1", "1"],
            "step_sizes": ["35", "35", "35", "1"],
            "n_linesteps": 1,
            "unidirectional": True,
        },
    }
    run = run_reconstruction(adapter, data_obj, params)
    node = _recon_node(run.result)
    assert node["plugin_id"] == "monalisa-legacy"
    assert node["params"]["bleaching_correction"] is True
    assert node["params"]["pattern"] == [1.0, 2.0, 3.0, 4.0]
    assert node["params"]["scan_params"]["steps"] == ["2", "2", "1", "1"]
    assert node["replayable"] is True
    assert run.result.data.shape == (1, 2, 1, 1, 6, 6)

    merged = run_consolidation(adapter, [run, run])
    assert output_node(merged)["op"] == "consolidate"
    assert merged.data.shape[0] == 2


# -- 5. live batch fallback ---------------------------------------------------------

def test_live_batch_fallback_records_a_memory_source_as_non_replayable():
    from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
    from imswitch.improcess.controller.LiveReconstructionController import (
        LiveReconstructionController,
    )

    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    comm = CommunicationChannel()
    produced = []
    comm.sigResultProduced.connect(lambda result, name: produced.append(result))
    controller = LiveReconstructionController(comm)
    controller._reconstructor = _Recon()
    controller._source = SimpleNamespace(name="live-source")
    controller._params = {"p": 1}
    controller._is_streaming = False
    controller._buffer = [Chunk(stack[:2], 0, 2), Chunk(stack[2:], 2, 4)]
    controller._stack_info = StackInfo(frame_shape=(3, 3), dtype=stack.dtype, attrs={"x": 1},
                                       expected_frames=4, frames_per_stack=4, detector_name="CAM")

    controller._on_batch_stack_complete()

    node = _recon_node(produced[0])
    source = graph_of(produced[0])["nodes"][node["inputs"][0]["node"]]
    assert source["source"]["kind"] == "memory"
    assert source["source"]["fingerprint"]["shape"] == [4, 3, 3]
    assert node["replayable"] is False
    assert "not persisted" in node["reasons"][0]


def test_a_memory_source_that_knows_its_recording_path_is_replayable(tmp_path):
    from imswitch.improcess.live.sources import InMemoryStackWrapper

    recording = tmp_path / "rec.h5"
    recording.write_bytes(b"0")
    wrapper = InMemoryStackWrapper("rec", "CAM", np.zeros((2, 3, 3)),
                                   attrs={"recording:dataset_path": str(recording)})
    node = _recon_node(run_reconstruction(_Recon(), wrapper, {}).result)
    source = graph_of(run_reconstruction(_Recon(), wrapper, {}).result)["nodes"][node["inputs"][0]["node"]]
    assert source["source"]["kind"] == "memory"
    assert source["source"]["path"] == str(recording)
    assert node["replayable"] is True


# -- 6. RAM recording ----------------------------------------------------------------

def test_ram_recording_path_records():
    import h5py

    from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
    from imswitch.improcess.controller.MemoryLiveController import MemoryLiveController

    controller = MemoryLiveController.__new__(MemoryLiveController)
    from unittest.mock import MagicMock

    controller._logger = MagicMock()
    comm = CommunicationChannel()
    produced = []
    comm.sigResultProduced.connect(lambda result, name: produced.append((result, name)))
    controller._commChannel = comm

    buf = BytesIO()
    with h5py.File(buf, "w") as handle:
        handle.create_dataset("data", data=np.arange(12).reshape(3, 2, 2))
    with h5py.File(buf, "r") as handle:
        controller._processDataset("ram-rec", "data", handle, _Recon(), {"q": 3})

    result, name = produced[0]
    assert name == "Live (RAM)"
    node = _recon_node(result)
    assert node["params"] == {"q": 3}
    assert node["replayable"] is False                     # never touched disk


# -- 7. streaming -----------------------------------------------------------------------

class _Session(StreamingSession):
    def __init__(self):
        self.frames = 0

    def begin(self, init_obj, params):
        return StreamPlan(out_shape=(4, 4), axis_labels=["Y", "X"], view_modes=[ViewMode("Standard", (0, 1))])

    def push(self, chunk, start, end):
        self.frames = end

    def result(self):
        return ArrayProcessingResult("live", np.full((4, 4), self.frames, dtype=np.float32), ["Y", "X"])


def _stream_worker(expected=6):
    from imswitch.improcess.live.workers import LiveProcessWorker

    session = _Session()
    worker = LiveProcessWorker(session, update_cadence=1)
    init = StreamInit(name="stream", dataset_name="CAM", data=np.zeros((2, 4, 4)),
                      stack_info=StackInfo(frame_shape=(4, 4), dtype=np.float32, expected_frames=expected))
    worker.setProvenance(_Recon(), {"s": 1}, init, expected_frames=expected)
    return worker, session


def test_streaming_snapshots_share_one_node_and_the_final_is_complete():
    worker, session = _stream_worker()
    snapshots, finals = [], []
    worker.sigResultUpdated.connect(snapshots.append)
    worker.sigStackFinished.connect(finals.append)

    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 0, 2))
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 2, 4))
    worker.finalize()

    first, second = (output_node(s) for s in snapshots)
    assert first["mode"] == "streaming" and first["completion"]["status"] == "partial"
    assert first["completion"]["frames_committed"] == 2
    assert second["completion"]["frames_committed"] == 4
    assert graph_of(snapshots[0])["output"]["node"] == graph_of(snapshots[1])["output"]["node"]
    final = output_node(finals[0])
    assert final["completion"] == {"status": "complete", "frames_committed": 4, "expected_frames": 6}
    assert final["replayable"] is False                     # live source, not a file
    assert "not persisted" in " ".join(final["reasons"])


def test_a_stalled_stream_says_so():
    worker, _ = _stream_worker()
    finals = []
    worker.sigStackFinished.connect(finals.append)
    worker.processChunk(Chunk(np.zeros((2, 4, 4)), 0, 2))
    worker.markStalled(12.0)
    worker.finalize()
    node = output_node(finals[0])
    assert node["completion"]["status"] == "stalled"
    assert any("stalled" in reason for reason in node["reasons"])


# -- 8. headless ------------------------------------------------------------------------

def test_headless_run_records_a_file_source_with_fingerprint(tmp_path):
    run = run_reconstruction(_Recon(), _file(tmp_path), {"h": 1})
    node = _recon_node(run.result)
    assert run.source["kind"] == "file" and run.source["fingerprint"]["size"] == 8
    assert node["replayable"] is True
    assert run.node == graph_of(run.result)["output"]


def test_context_is_passed_only_when_given(tmp_path):
    seen = {}

    class _Strict(Reconstructor):
        name = "strict"
        id = "t.strict"

        def make_param_widget(self, parent):
            return None

        def make_metadata_dialog(self, parent):
            return None

        def process(self, data_obj, params):          # no context parameter at all
            seen["called"] = True
            return ArrayProcessingResult("r", np.zeros((2, 2)), ["Y", "X"])

    run_reconstruction(_Strict(), _file(tmp_path), {})
    assert seen["called"]
    with pytest.raises(TypeError):
        run_reconstruction(_Strict(), _file(tmp_path), {}, context=object())

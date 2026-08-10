"""Phase 2e contracts for worker execution, resources and cancellation."""

import weakref

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import tifffile
from qtpy import QtCore, QtWidgets

import imswitch.imcommon.algorithms.tile_mosaic as tile_mosaic
from imswitch.imcommon.algorithms.detector_transform import (
    parse_detector_transform,
)
from imswitch.imcommon.algorithms.tile_mosaic import (
    AlignmentArtifact,
    IndexedTile,
    ManifestPayloadRef,
    MosaicLayout,
    PayloadAssemblyOptions,
    PayloadSelection,
    RefinementReport,
    TilingDatasetIndex,
    assemble_payload,
    manifest_fingerprint,
)
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)
from imswitch.improcess.controller.reconstruction_worker import (
    ReconstructionWorker,
    ReconstructionWorkerJob,
)
from imswitch.improcess.reconstructors.base import (
    CancellationToken,
    ReconstructionCancelled,
    ReconstructionContext,
    Reconstructor,
    ResourceEstimate,
)
from imswitch.improcess.reconstructors.tiling import TilingReconstructor


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _indexed_run(tmp_path: Path, data=None):
    data = (
        np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
        if data is None else np.asarray(data)
    )
    manifest = tmp_path / "tiles.json"
    manifest.write_text("{}", encoding="utf-8")
    alignment_path = tmp_path / "alignment.tiff"
    payload_path = tmp_path / "payload.tiff"
    tifffile.imwrite(alignment_path, np.ones((4, 5), np.uint16))
    tifffile.imwrite(payload_path, data, photometric="minisblack")
    alignment = AlignmentArtifact(
        alignment_path, "Camera", "YX", "YX", (4, 5), (4, 5)
    )
    payload = ManifestPayloadRef(
        path=payload_path,
        group=None,
        detector="Camera",
        axes="CZYX",
        stored_axes="CZYX",
        shape=tuple(data.shape),
        stored_shape=tuple(data.shape),
        generation=1,
        complete=True,
        transform_to_alignment=parse_detector_transform("identity"),
    )
    tile = IndexedTile(
        tile_id=0,
        grid=(0, 0),
        stage_um=(0.0, 0.0),
        saved_position_yx=(0.0, 0.0),
        alignment=alignment,
        payloads={"Camera": payload},
    )
    index = TilingDatasetIndex(
        manifest=manifest,
        alignment_detector="Camera",
        pixel_size_yx_um=(1.0, 1.0),
        z_step_um=0.5,
        tiles=(tile,),
        detectors=("Camera",),
        format="imswitch-tiling/2",
    )
    layout = MosaicLayout(
        {0: (0.0, 0.0)}, "saved", RefinementReport(tiles=1)
    )
    return index, layout


def _params(**overrides):
    params = {
        "refine": False,
        "blend": True,
        "stage_positions": False,
        "detector": "Camera",
        "channel": None,
        "project_z": False,
        "max_shift_px": None,
    }
    params.update(overrides)
    return params


def test_execution_policy_is_opt_in():
    assert Reconstructor.execution_policy == "inline"
    assert TilingReconstructor.execution_policy == "worker"


def test_context_progress_is_monotonic_and_cancellation_is_cooperative():
    updates = []
    token = CancellationToken()
    context = ReconstructionContext(
        progress_callback=updates.append,
        cancellation_token=token,
    )

    context.report("inspect", 1, 1, "parsed")
    context.report("align", 8, 10, "pairs")
    # Discovering a larger denominator must not move visible progress backward.
    context.report("align", 1, 100, "more pairs")
    assert [item.fraction for item in updates] == sorted(
        item.fraction for item in updates
    )

    token.cancel()
    with pytest.raises(ReconstructionCancelled):
        context.check_cancelled()


def test_exact_allocation_refuses_unconfirmed_over_budget_work(tmp_path):
    index, layout = _indexed_run(tmp_path)

    with pytest.raises(MemoryError) as error:
        assemble_payload(
            index,
            layout,
            PayloadSelection("Camera"),
            PayloadAssemblyOptions(memory_budget_bytes=1),
        )

    message = str(error.value)
    assert "output shape" in message
    assert "canvas" in message and "weights" in message
    assert "Select one channel" in message
    assert "Max-project Z" in message


def test_tiling_reports_all_named_phases_without_loading_every_tile(tmp_path):
    index, _layout = _indexed_run(tmp_path)
    updates = []
    data_obj = SimpleNamespace(
        name="run",
        dataPath=str(index.manifest),
        sourceMetadata=index,
        sourceFingerprint=manifest_fingerprint(index.manifest),
    )
    context = ReconstructionContext(
        progress_callback=updates.append,
        memory_budget_bytes=1024 ** 3,
    )

    result = TilingReconstructor().process(data_obj, _params(), context=context)

    assert result.data.shape == (2, 3, 4, 5)
    assert {item.phase for item in updates} == {
        "inspect", "align", "allocate", "assemble", "finalize"
    }
    assert [item.fraction for item in updates] == sorted(
        item.fraction for item in updates
    )
    assert updates[-1].fraction == 1.0


def test_payload_assembly_releases_each_synthetic_tile_before_next(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "tiles.json"
    manifest.write_text("{}", encoding="utf-8")
    alignment = AlignmentArtifact(
        tmp_path / "alignment.tiff", "Camera", "YX", "YX", (512, 512),
        (512, 512)
    )
    tiles = []
    for tile_id in range(3):
        payload = ManifestPayloadRef(
            path=tmp_path / f"payload-{tile_id}.tiff",
            group=None,
            detector="Camera",
            axes="CYX",
            stored_axes="CYX",
            shape=(2, 512, 512),
            stored_shape=(2, 512, 512),
            generation=1,
            complete=True,
            transform_to_alignment=parse_detector_transform("identity"),
        )
        tiles.append(IndexedTile(
            tile_id=tile_id,
            grid=(0, tile_id),
            stage_um=(0.0, float(tile_id * 512)),
            saved_position_yx=(0.0, float(tile_id * 512)),
            alignment=alignment,
            payloads={"Camera": payload},
        ))
    index = TilingDatasetIndex(
        manifest=manifest,
        alignment_detector="Camera",
        pixel_size_yx_um=(1.0, 1.0),
        z_step_um=None,
        tiles=tuple(tiles),
        detectors=("Camera",),
        format="imswitch-tiling/2",
    )
    layout = MosaicLayout(
        {tile.tile_id: tile.saved_position_yx for tile in tiles},
        "saved",
        RefinementReport(tiles=len(tiles)),
    )

    live_tiles = []

    def read_synthetic_payload(ref):
        if live_tiles:
            assert live_tiles[-1]() is None
        array = np.full(ref.shape, ref.generation, dtype=np.float32)
        live_tiles.append(weakref.ref(array))
        return array

    monkeypatch.setattr(
        tile_mosaic, "inspect_manifest_payload", lambda _ref: None
    )
    monkeypatch.setattr(
        tile_mosaic, "read_manifest_payload", read_synthetic_payload
    )

    result = assemble_payload(
        index, layout, PayloadSelection("Camera"), PayloadAssemblyOptions()
    )

    assert result.data.shape == (2, 512, 1536)
    assert len(live_tiles) == 3
    assert all(reference() is None for reference in live_tiles)


def test_cancelled_worker_publishes_no_partial_result():
    class _CancellingReconstructor:
        def process(self, data_obj, params, context=None):
            context.report("inspect", 1, 1, "ready")
            context.cancellation_token.cancel()
            context.check_cancelled()

    worker = ReconstructionWorker(
        _CancellingReconstructor(),
        [ReconstructionWorkerJob(object(), {}, 1024)],
    )
    finished, cancelled, failed = [], [], []
    worker.finished.connect(finished.append)
    worker.cancelled.connect(cancelled.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert finished == []
    assert failed == []
    assert len(cancelled) == 1


def test_default_inline_dispatch_keeps_old_process_call_shape():
    result = SimpleNamespace(name="inline", output_pixel_size_nm=None)
    reconstructor = SimpleNamespace(
        id="plain",
        name="Plain",
        execution_policy="inline",
        supports_consolidation=False,
        process=MagicMock(return_value=result),
    )
    data_obj = SimpleNamespace(name="input")
    controller = ReconstructorManagerController.__new__(
        ReconstructorManagerController
    )
    QtCore.QObject.__init__(controller)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {"value": 7}
    controller._main = SimpleNamespace(
        _activeReconstructor=reconstructor,
        monalisaController=MagicMock(),
        wfsBatchController=MagicMock(),
    )
    controller._commChannel = MagicMock()

    controller._reconstruct_with_plugin([data_obj], consolidate=False)

    reconstructor.process.assert_called_once_with(data_obj, {"value": 7})


def test_memory_confirmation_happens_before_worker_launch():
    estimate = ResourceEstimate((100, 100), 800, 400, "test mosaic")
    reconstructor = SimpleNamespace(
        id="worker",
        name="Worker",
        estimate_resources=lambda _data, _params: estimate,
    )
    states = []
    controller = ReconstructorManagerController.__new__(
        ReconstructorManagerController
    )
    controller._reconstructionThread = None
    controller._reconstructionWorker = None
    controller._reconstructionWorkerReconstructor = None
    controller._logger = MagicMock()
    controller._widget = SimpleNamespace(
        getReconstructionParams=lambda: {},
        confirmReconstructionMemory=lambda _estimate, _budget: False,
        setReconstructionJobState=lambda running, **kwargs: states.append(
            (running, kwargs)
        ),
    )
    controller._main = SimpleNamespace(monalisaController=MagicMock())
    controller._default_memory_budget_bytes = lambda: 1000

    controller._start_worker_reconstruction(
        reconstructor, [SimpleNamespace(name="input")], False
    )

    assert controller._reconstructionThread is None
    assert states[-1][0] is False
    assert "declined" in states[-1][1]["status"]


def test_worker_policy_runs_off_gui_thread_and_publishes_on_gui(qapp):
    gui_thread = QtCore.QThread.currentThread()
    process_threads = []
    published = []
    states = []

    class _WorkerReconstructor:
        id = "worker"
        name = "Worker"
        execution_policy = "worker"
        supports_consolidation = False

        def estimate_resources(self, data_obj, params):
            return None

        def process(self, data_obj, params, context=None):
            process_threads.append(QtCore.QThread.currentThread())
            for phase in (
                "inspect", "align", "allocate", "assemble", "finalize"
            ):
                context.report(phase, 1, 1, phase)
            return SimpleNamespace(name="worker-result", output_pixel_size_nm=None)

    reconstructor = _WorkerReconstructor()
    controller = ReconstructorManagerController.__new__(
        ReconstructorManagerController
    )
    QtCore.QObject.__init__(controller)
    controller._logger = MagicMock()
    controller._widget = SimpleNamespace(
        getReconstructionParams=lambda: {},
        setReconstructionJobState=lambda running, **kwargs: states.append(
            (running, kwargs)
        ),
        parTree=object(),
    )
    controller._main = SimpleNamespace(
        _activeReconstructor=reconstructor,
        monalisaController=MagicMock(),
        wfsBatchController=MagicMock(),
    )
    controller._commChannel = SimpleNamespace(
        sigResultProduced=SimpleNamespace(
            emit=lambda result, name: published.append((result, name))
        ),
        sigCurrentResultChanged=SimpleNamespace(emit=lambda _result: None),
    )
    controller._reconstructionThread = None
    controller._reconstructionWorker = None
    controller._reconstructionWorkerReconstructor = None
    controller._default_memory_budget_bytes = lambda: 1024 ** 3

    loop = QtCore.QEventLoop()
    original_state = controller._widget.setReconstructionJobState

    def state(running, **kwargs):
        original_state(running, **kwargs)
        if not running and kwargs.get("status") == "Reconstruction complete.":
            QtCore.QTimer.singleShot(0, loop.quit)

    controller._widget.setReconstructionJobState = state
    controller._reconstruct_with_plugin(
        [SimpleNamespace(name="input")], consolidate=False
    )
    QtCore.QTimer.singleShot(3000, loop.quit)
    loop.exec_()
    qapp.processEvents()

    assert process_threads and process_threads[0] is not gui_thread
    assert published and published[0][1] == "worker-result"
    assert states[-1][0] is False

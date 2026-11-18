"""M2D refinement: finite jobs never inherit another acquisition's Auto fit."""

from types import SimpleNamespace

import pytest

from imswitch.improcess.execution.localization_scope import fresh_acquisition_params
from imswitch.improcess.execution.policy import ExecutionPath, choose_execution
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement, ProcessingJob, ProcessingOrigin,
)


def _plugin(*, streaming=True, progressive=True):
    return SimpleNamespace(
        id="monalisa", supports_streaming=streaming,
        supports_progressive_finite=progressive,
        supports_streaming_params=lambda params: streaming,
    )


def _job(*, mode=ExecutionRequirement.FINITE, origin=ProcessingOrigin.MANUAL):
    return ProcessingJob(dataset_id="lapse", label="lapse", reconstructor_id="monalisa",
                         params={"preset": "Fast"}, origin=origin,
                         execution_requirement=mode)


@pytest.mark.parametrize("origin,requirement", [
    (ProcessingOrigin.MANUAL, ExecutionRequirement.FINITE),
    (ProcessingOrigin.AUTO, ExecutionRequirement.POLICY),
    (ProcessingOrigin.AUTO, ExecutionRequirement.FOLLOW),
])
def test_completed_timelapse_uses_finite_worker_even_with_fast_streaming(origin, requirement):
    job = _job(mode=requirement, origin=origin)
    assert choose_execution(job, "time-lapse", _plugin()).path == ExecutionPath.OFFLINE_FINITE


def test_finite_policy_never_even_queries_streaming_capability():
    plugin = SimpleNamespace(
        supports_streaming=True,
        supports_streaming_params=lambda params: (_ for _ in ()).throw(
            AssertionError("Finite jobs must not probe the live executor")
        ),
    )
    assert choose_execution(_job(), "time-lapse", plugin).path == ExecutionPath.OFFLINE_FINITE


def test_only_growing_explicit_follow_selects_streaming():
    auto = _job(mode=ExecutionRequirement.FOLLOW, origin=ProcessingOrigin.AUTO)
    assert choose_execution(auto, "time-lapse", _plugin(), growing=True,
                            live_reader=True).path == ExecutionPath.INCREMENTAL_FOLLOW
    assert choose_execution(auto, "time-lapse", _plugin(), growing=True,
                            live_reader=False).path == ExecutionPath.WAIT_FINITE
    manual = _job()
    assert choose_execution(manual, "time-lapse", _plugin(), growing=True,
                            live_reader=True).path == ExecutionPath.WAIT_FINITE


def test_auto_params_reset_for_independent_job_without_mutating_widget():
    original = {"localization_mode": "Auto", "auto_localization": {"period": 9.9},
                "locked_localization": {"period": 5.5}}
    prepared = fresh_acquisition_params(_plugin(), original)
    assert prepared["auto_localization"] == {}
    assert original["auto_localization"] == {"period": 9.9}
    assert prepared["locked_localization"] == {"period": 5.5}


@pytest.mark.parametrize("mode", ["Manual", "Locked"])
def test_explicit_calibration_is_not_reset(mode):
    original = {"localization_mode": mode, "auto_localization": {"old": 1},
                "locked_localization": {"chosen": 2}}
    assert fresh_acquisition_params(_plugin(), original) == original


def test_other_plugins_are_not_affected():
    original = {"localization_mode": "Auto", "auto_localization": {"period": 8.0}}
    assert fresh_acquisition_params(SimpleNamespace(id="other"), original) == original


def test_queue_snapshot_applies_independent_acquisition_scope():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    params = {"localization_mode": "Auto", "auto_localization": {"period": 8.0}}
    manager = SimpleNamespace(snapshotProcessingParams=lambda recon: params)
    queue_stub = SimpleNamespace(_reconstructorManager=manager)
    for _ in range(2):
        snapshot = ProcessingQueueController._snapshot_params(queue_stub, _plugin())
        assert snapshot["auto_localization"] == {}
    assert params["auto_localization"]["period"] == 8.0


def test_monalisa_first_result_provides_small_timepoint_only_override():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    result = SimpleNamespace(metadata={"monalisa_calibration": {
        "resolved_localization": {"period": 10.2, "source": "auto"}
    }})
    override = MonalisaReconstructor.finite_timepoint_reuse_overrides(
        result, {"localization_mode": "Auto"}
    )
    assert override == {"auto_localization": {"period": 10.2, "source": "auto"}}
    assert MonalisaReconstructor.finite_timepoint_reuse_overrides(
        result, {"localization_mode": "Manual"}
    ) == {}
    assert MonalisaReconstructor.finite_timepoint_reuse_overrides(
        result, {"localization_mode": "Locked"}
    ) == {}


def test_monalisa_missing_first_auto_localization_relocalizes_safely():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    assert MonalisaReconstructor.finite_timepoint_reuse_overrides(
        SimpleNamespace(metadata={}), {"localization_mode": "Auto"}
    ) == {}


def test_worker_reuses_first_timepoint_calibration_only_within_same_lapse(monkeypatch):
    import imswitch.improcess.controller.reconstruction_worker as module
    from imswitch.improcess.controller.reconstruction_worker import (
        ReconstructionWorker, ReconstructionWorkerJob,
    )
    captured = []
    plugin = SimpleNamespace(
        finite_timepoint_reuse_overrides=lambda result, params: {
            "auto_localization": dict(result.metadata["resolved"])
        },
    )

    def run_plugin(reconstructor, source, params, *, context):
        captured.append((source.name, dict(params)))
        # Per-acquisition first result is deliberately distinct.
        return SimpleNamespace(result=SimpleNamespace(
            metadata={"resolved": {"source_name": source.name}}
        ))

    monkeypatch.setattr(module, "run_reconstruction", run_plugin)
    jobs = [ReconstructionWorkerJob(SimpleNamespace(name=f"T{i}"),
                                    {"localization_mode": "Auto", "auto_localization": {}},
                                    None) for i in range(3)]
    worker = ReconstructionWorker(plugin, jobs, reuse_timepoint_calibration=True,
                                  retain_results=False)
    failures = []
    worker.failed.connect(lambda value: failures.append(value.message))
    worker.run()
    assert not failures
    assert captured[0][1]["auto_localization"] == {}
    assert captured[1][1]["auto_localization"] == {"source_name": "T0"}
    assert captured[2][1]["auto_localization"] == {"source_name": "T0"}

    captured.clear()
    independent = ReconstructionWorker(plugin, jobs[:2],
                                       reuse_timepoint_calibration=False,
                                       retain_results=False)
    independent.run()
    assert [params["auto_localization"] for _, params in captured] == [{}, {}]

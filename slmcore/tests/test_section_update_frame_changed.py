import numpy as np

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.cgh import CGHResult
from slmcore.cgh.execution.status import CGHResultState
from slmcore.engine.section import split_slm_geometry


def _runtime() -> SLMRuntime:
    geometry = SLMGeometry(width=32,height=32,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )


def _commit_test_cgh_result(runtime: SLMRuntime,section_key: str) -> None:
    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci",
        },
    )
    job = runtime.prepare_section_cgh(section_key)
    yy,xx = np.indices(job.spec.context.shape,dtype=np.float64)
    pattern = np.exp(1j*2*np.pi*(xx + yy)/(2*job.spec.context.shape[1]))
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=pattern,
    )

    assert runtime.commit_section_cgh(section_key,result) is not None


def test_section_update_reports_frame_changed_for_visual_patch():
    runtime = _runtime()
    section_key = "sec_0"
    runtime.apply_section_patch(
        section_key,
        {
            ("patterns","active"):True,
            ("patterns","linear_phase","params","active"):True,
        },
    )
    before_artifacts = runtime.artifacts

    update = runtime.apply_section_patch(
        section_key,
        {("patterns","linear_phase","params","period_x"):8},
    )

    assert update is not None
    assert update.frame_changed
    assert runtime.artifacts is not before_artifacts


def test_stale_cgh_parameter_update_does_not_recompose_aggregate_frame():
    runtime = _runtime()
    section_key = "sec_0"
    _commit_test_cgh_result(runtime,section_key)
    before_artifacts = runtime.artifacts
    before_frame = runtime.artifacts.eightbit

    update = runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci","computation","params","n_iterations"):49},
    )

    assert update is not None
    assert not update.frame_changed
    assert update.cgh_status.result_state is CGHResultState.STALE
    assert runtime.artifacts is before_artifacts
    assert runtime.artifacts.eightbit is before_frame

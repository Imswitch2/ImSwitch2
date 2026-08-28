from __future__ import annotations

import numpy as np

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.cgh import CGHResult,CGHResultState,CGHSignature
from slmcore.core.cgh.execution import CGHSpec
from slmcore.core.cgh.targets.lattice import LatticeLockRequest
from slmcore.core.engine.section import split_slm_geometry
from slmcore.core.engine.section.context import SectionContext
from slmcore.core.engine.section.geometry import SectionGeometry


def _runtime(size=256) -> tuple[SLMRuntime,str]:
    geometry = SLMGeometry(width=size,height=size,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    section_key = "sec_0"
    runtime.apply_section_patch(
        section_key,{
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci",
        },
    )
    return runtime,section_key


def _commit_job(runtime: SLMRuntime,section_key: str):
    job = runtime.prepare_section_cgh(section_key)
    pattern = np.ones(job.spec.context.shape,dtype=np.complex128)
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=pattern,
    )
    assert runtime.commit_section_cgh(section_key,result) is not None
    return job


def test_policy_only_change_keeps_base_target_and_cgh_current():
    runtime,section_key = _runtime()
    original_job = _commit_job(runtime,section_key)

    update = runtime.apply_section_patch(
        section_key,{
            ("cgh","multi_foci","params","resolution_priority"):
                "foci_count",
        },
    )

    assert update is not None
    assert not update.target_definition_changed
    assert runtime.get_section_cgh_status(
        section_key
    ).result_state is CGHResultState.CURRENT

    next_job = runtime.prepare_section_cgh(section_key)
    assert next_job.spec.target_params["resolution_priority"] == "foci_count"
    assert next_job.spec.feedback_target_signature == (
        original_job.spec.feedback_target_signature
    )
    assert next_job.spec.signature == original_job.spec.signature


def test_lock_only_change_keeps_base_target_identity():
    runtime,section_key = _runtime()

    update = runtime.apply_section_patch(
        section_key,{},
        lattice_lock_request=LatticeLockRequest(
            "multi_foci","fov",(180.0,180.0),
        ),
    )

    assert update is not None
    assert not update.target_definition_changed


def test_policy_change_that_resolves_different_lattice_changes_target():
    runtime,section_key = _runtime()
    runtime.apply_section_patch(
        section_key,{
            ("cgh","multi_foci","params","period_x_px"):8.1,
            ("cgh","multi_foci","params","period_y_px"):8.1,
        },
    )
    runtime.apply_section_patch(
        section_key,{},
        lattice_lock_request=LatticeLockRequest(
            "multi_foci","fov",(180.0,180.0),
        ),
    )
    _commit_job(runtime,section_key)

    update = runtime.apply_section_patch(
        section_key,{
            ("cgh","multi_foci","params","resolution_priority"):"fov",
        },
    )

    assert update is not None
    assert update.target_definition_changed
    assert runtime.get_section_cgh_status(
        section_key
    ).result_state is CGHResultState.STALE


def test_cgh_spec_target_params_are_provenance_not_identity():
    context = SectionContext(
        geometry=SectionGeometry(
            key="sec_0",x=0,y=0,width=64,height=64,
        ),
        pixel_size_um=1.0,
        wavelength_nm=488,
        pupil_radius_px=32,
        center_offset_x_px=0,
        center_offset_y_px=0,
    )
    feedback_signature = CGHSignature("same-feedback-target")
    common = dict(
        context=context,
        target_type="multi_foci",
        algorithm="gerchberg_saxton",
        compute_params={"iterations":10},
        feedback_target_signature=feedback_signature,
    )

    period = CGHSpec(
        target_params={"resolution_priority":"period"},
        **common,
    )
    fov = CGHSpec(
        target_params={"resolution_priority":"fov"},
        **common,
    )

    assert period.target_params != fov.target_params
    assert period.signature == fov.signature


def test_restore_current_cgh_target_resynchronizes_stale_target_without_recompute():
    runtime,section_key = _runtime()
    original_job = _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,{
            ("cgh","multi_foci","params","period_x_px"):8.1,
        },
    )
    stale = runtime.get_section_cgh_status(section_key)
    assert stale.result_state is CGHResultState.STALE
    assert stale.draft_target_changed
    assert stale.target_restore_available

    update = runtime.restore_section_current_cgh_target(section_key)

    assert update is not None
    assert update.target_definition_changed
    restored = runtime.get_section_cgh_status(section_key)
    assert restored.result_state is CGHResultState.CURRENT
    assert not restored.draft_target_changed
    assert runtime.get_section_parameter(
        section_key,("cgh","multi_foci","params","period_x_px"),
    ) == original_job.spec.target_params["period_x_px"]


def test_restore_current_cgh_target_preserves_lattice_lock_reference():
    runtime,section_key = _runtime()
    runtime.apply_section_patch(
        section_key,{},
        lattice_lock_request=LatticeLockRequest(
            "multi_foci","fov",(180.0,180.0),
        ),
    )
    _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,{
            ("cgh","multi_foci","params","period_x_px"):8.1,
        },
    )
    assert runtime.get_section_cgh_status(section_key).draft_target_changed

    update = runtime.restore_section_current_cgh_target(section_key)

    assert update is not None
    assert runtime.get_section_cgh_status(
        section_key
    ).result_state is CGHResultState.CURRENT
    lock = runtime.get_section_snapshot(
        section_key
    ).state.cgh.items["multi_foci"].lock_state
    assert lock.kind == "fov"
    assert lock.reference == (180.0,180.0)


def test_restore_current_cgh_target_restores_target_type():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,{
            ("cgh","selected_target"):"multi_foci_vector",
        },
    )
    stale = runtime.get_section_cgh_status(section_key)
    assert stale.result_state is CGHResultState.STALE
    assert stale.draft_target_changed
    assert stale.target_restore_available

    update = runtime.restore_section_current_cgh_target(section_key)

    assert update is not None
    snapshot = runtime.get_section_snapshot(section_key)
    assert snapshot.state.cgh.selected_target == "multi_foci"
    restored = runtime.get_section_cgh_status(section_key)
    assert restored.result_state is CGHResultState.CURRENT
    assert not restored.draft_target_changed


def test_context_staleness_is_not_target_restorable():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,{("optics","wavelength_nm"):500},
    )

    status = runtime.get_section_cgh_status(section_key)
    assert status.result_state is CGHResultState.STALE
    assert status.draft_target_changed
    assert not status.target_restore_available

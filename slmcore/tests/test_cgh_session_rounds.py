from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.cgh import (
    FeedbackChangeKind,
    CGHResult,
    CGHResultState,
    CGHSignature,
    CGHWorkingRoundState,
)
from slmcore.cgh.execution import CGHPreparedPurpose
from slmcore.cgh.localization import LocalizationResult
from slmcore.measurement import ImageMeasurement
from slmcore.engine.section import split_slm_geometry


def _runtime() -> tuple[SLMRuntime,str]:
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    section_key = "sec_0"
    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","n_foci_x"):2,
            ("cgh","multi_foci_vector","params","n_foci_y"):2,
        },
    )
    return runtime,section_key


def _commit_job(runtime: SLMRuntime,section_key: str):
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
    return job,result


def _current_resolution(runtime: SLMRuntime,section_key: str):
    section = runtime._get_section(section_key)
    return section._cgh_session.create_target_resolution(
        section.state.cgh,
        section._build_context(section.state),
    )


def _attach_localized_measurement(
    runtime: SLMRuntime,
    section_key: str,
    powers=(1.0,1.0,0.25,1.0),
):
    resolution = _current_resolution(runtime,section_key)
    n_spots = int(resolution.lattice_indices.shape[1])
    image = np.zeros((64,64),dtype=np.float64)
    expected = np.array([
        [16.0,48.0,16.0,48.0],
        [16.0,16.0,48.0,48.0],
    ],dtype=np.float64)
    expected = expected[:,:n_spots]
    powers = tuple(powers)[:n_spots]
    for (x,y),power in zip(expected.T,powers):
        image[int(y),int(x)] = power

    measurement = ImageMeasurement(image=image,source="test")
    runtime.set_section_feedback_measurement(section_key,measurement)
    params = dict(
        runtime.get_section_feedback_status(section_key).localization_params
    )
    localization = LocalizationResult(
        target_type="multi_foci_vector",
        target_params=dict(resolution.canonical_params),
        parameters=params,
        lattice_indices=resolution.lattice_indices,
        crop_coord=(0,image.shape[0],0,image.shape[1]),
        cropped_image=image,
        expected_positions_px=expected,
        measured_positions_px=expected,
        period_x_px=32.0,
        period_y_px=32.0,
        offset_x_px=16.0,
        offset_y_px=16.0,
        diagnostics={
            "measurement_id":measurement.measurement_id,
            "matched_mask":tuple(True for _ in range(n_spots)),
            "matched_count":n_spots,
            "missing_count":0,
        },
    )
    runtime.commit_section_feedback_localization(
        section_key,localization,params,
    )
    analysis = runtime.compute_section_feedback_intensity_analysis(
        section_key,localization,
    )
    runtime.set_section_feedback_intensity_analysis(section_key,analysis)
    return measurement,localization


def _commit_next_intensity_round(runtime: SLMRuntime,section_key: str):
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    return _commit_job(runtime,section_key)


def test_round_zero_is_committed_complete_round():
    runtime,section_key = _runtime()

    _job,result = _commit_job(runtime,section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert status.current_round_index == 0
    assert status.working_round_index is None
    assert len(inspection.rounds) == 1
    assert inspection.rounds[0].index == 0
    assert inspection.rounds[0].result is not None
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        result.pattern,
    )


def test_intensity_adapt_creates_working_round_and_commit_promotes_it():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    transition = runtime.apply_section_intensity_feedback(section_key)

    assert transition is not None
    status = runtime.get_section_cgh_status(section_key)
    feedback = runtime.get_section_feedback_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.STALE
    assert status.current_round_index == 0
    assert status.working_round_index == 1
    assert status.working_round_state is CGHWorkingRoundState.NOT_COMPUTED
    assert feedback.adaptation_pending
    assert feedback.feedback_compute_pending
    assert feedback.pending_feedback_change is FeedbackChangeKind.INTENSITY
    assert len(inspection.rounds) == 1
    assert inspection.rounds[0].evaluation is not None
    assert inspection.rounds[0].evaluation.intensity_analysis is not None
    assert inspection.working_round is not None
    assert inspection.working_round.result is None

    _commit_job(runtime,section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert status.current_round_index == 1
    assert status.working_round_index is None
    assert len(inspection.rounds) == 2
    assert inspection.rounds[1].adaptation is not None
    assert inspection.rounds[1].result is not None


def test_equal_intensity_adaptation_still_advances_revision_and_round():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    before_revision = runtime.get_section_snapshot(section_key).revision
    _attach_localized_measurement(runtime,section_key,powers=(1.0,1.0,1.0,1.0))

    transition = runtime.apply_section_intensity_feedback(section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert transition is not None
    assert transition.snapshot.revision == before_revision + 1
    assert status.result_state is CGHResultState.STALE
    assert status.working_round_index == 1
    assert inspection.working_round is not None
    np.testing.assert_array_equal(
        inspection.working_round.intensities,
        inspection.rounds[0].intensities,
    )


def test_draft_target_edit_is_transactional_until_successful_compute():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    before = runtime.get_section_cgh_session_inspection(section_key)

    update = runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )

    assert update is not None
    assert update.cgh_status.result_state is CGHResultState.STALE
    assert len(runtime.get_section_cgh_session_inspection(section_key).rounds) == 1

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):2},
    )

    status = runtime.get_section_cgh_status(section_key)
    after = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert len(after.rounds) == 1
    assert after.rounds[0].result is before.rounds[0].result


def test_target_replacement_success_commits_new_round_zero():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )
    _commit_job(runtime,section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert status.current_round_index == 0
    assert len(inspection.rounds) == 1
    assert inspection.committed_target.canonical_params["n_foci_x"] == 3
    assert not inspection.position_active
    assert inspection.position_correction is None


def test_position_reset_keeps_previous_applied_cgh_stale():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    previous = runtime.get_section_cgh_result_copy(section_key)

    transition = runtime.apply_section_position_correction(section_key)

    assert transition is not None
    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.STALE
    assert status.current_round_index is None
    assert status.working_round_index == 0
    assert runtime.get_section_cgh_result_copy(section_key) is not None
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        previous.pattern,
    )
    assert len(inspection.rounds) == 0
    assert inspection.working_round is not None
    assert inspection.position_active


def test_position_correction_after_round_zero_needs_no_reset_confirmation():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    transition = runtime.apply_section_position_correction(section_key)

    assert transition is not None
    status = runtime.get_section_cgh_status(section_key)
    assert status.result_state is CGHResultState.STALE
    assert status.working_round_index == 0


def test_position_correction_with_intensity_history_requires_reset_flag():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    try:
        runtime.apply_section_position_correction(section_key)
    except RuntimeError as error:
        assert "reset" in str(error).lower()
    else:
        raise AssertionError("Expected position correction to require reset")

    assert runtime.apply_section_position_correction(
        section_key,reset_intensity=True,
    ) is not None
    assert runtime.get_section_cgh_status(section_key).working_round_index == 0


def test_invalidated_late_result_is_rejected():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    job = runtime.prepare_section_cgh(section_key)
    assert runtime.invalidate_section_cgh_compute(section_key)
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    )

    assert runtime.commit_section_cgh(section_key,result) is None


def test_late_failed_generation_does_not_mark_current_working_round_failed():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)

    job = runtime.prepare_section_cgh(section_key)
    assert runtime.invalidate_section_cgh_compute(section_key)

    assert not runtime.mark_section_cgh_compute_failed(
        section_key,job.generation,"late failure",
    )
    status = runtime.get_section_cgh_status(section_key)
    assert status.working_round_index == 1
    assert status.working_round_state is CGHWorkingRoundState.NOT_COMPUTED


def test_pure_intensity_reset_preserves_computed_round_zero():
    runtime,section_key = _runtime()
    _job0,result0 = _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)

    assert runtime.reset_section_intensity_feedback(section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert status.current_round_index == 0
    assert status.working_round_index is None
    assert len(inspection.rounds) == 1
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        result0.pattern,
    )


def test_reset_to_historical_round_truncates_future_rounds():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    _job2,result2 = _commit_next_intensity_round(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)

    assert runtime.reset_section_cgh_to_round(section_key,2)

    inspection = runtime.get_section_cgh_session_inspection(section_key)
    status = runtime.get_section_cgh_status(section_key)
    assert [round_record.index for round_record in inspection.rounds] == [0,1,2]
    assert status.current_round_index == 2
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        result2.pattern,
    )

    _commit_next_intensity_round(runtime,section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert [round_record.index for round_record in inspection.rounds] == [0,1,2,3]


def test_recompute_replaces_current_round_without_incrementing_round_index():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)

    before = runtime.get_section_cgh_session_inspection(section_key)
    assert len(before.rounds) == 2

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","computation","params","n_iterations"):25},
    )
    _job,result = _commit_job(runtime,section_key)

    after = runtime.get_section_cgh_session_inspection(section_key)
    assert len(after.rounds) == 2
    assert after.rounds[-1].index == 1
    assert after.rounds[-1].result is not before.rounds[-1].result
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        result.pattern,
    )


def test_failed_target_replacement_is_discarded_when_draft_returns_to_committed():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(
        section_key,
    )
    _commit_job(runtime,section_key)
    before = runtime.get_section_cgh_session_inspection(section_key)
    before_result = runtime.get_section_cgh_result_copy(section_key)

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )
    job = runtime.prepare_section_cgh(section_key)
    assert runtime.mark_section_cgh_compute_failed(
        section_key,job.generation,"boom",
    )

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.STALE
    assert inspection.working_round is not None
    assert inspection.working_round.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):2},
    )

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert inspection.working_round is None
    assert len(inspection.rounds) == len(before.rounds)
    assert inspection.position_active == before.position_active
    assert inspection.position_correction is before.position_correction
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy(section_key).pattern,
        before_result.pattern,
    )

    _attach_localized_measurement(runtime,section_key)
    assert runtime.apply_section_intensity_feedback(section_key) is not None
    assert runtime.get_section_cgh_status(section_key).working_round_index == 1

    late = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    )
    assert runtime.commit_section_cgh(section_key,late) is None


def test_working_round_is_excluded_from_persistence():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    runtime.prepare_section_cgh(section_key)

    live = runtime.get_section_cgh_session_inspection(section_key)
    assert live.working_round is not None
    assert live.working_round.state == CGHWorkingRoundState.COMPUTING.value

    loaded = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )

    restored = loaded.get_section_cgh_session_inspection(section_key)
    assert len(restored.rounds) == 1
    assert restored.working_round is None


def test_multi_round_session_snapshot_roundtrips_with_position_and_evaluations():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    config = runtime.create_config()
    restored = SLMRuntime.from_config(config,registries=DEFAULT_REGISTRIES)
    inspection = restored.get_section_cgh_session_inspection(section_key)

    assert inspection.committed_target is not None
    assert inspection.position_active
    assert inspection.position_correction is not None
    assert inspection.working_round is None
    assert [round_record.index for round_record in inspection.rounds] == [0,1,2]
    assert all(round_record.result is not None for round_record in inspection.rounds)
    assert inspection.rounds[1].adaptation is not None
    assert inspection.rounds[2].adaptation is not None
    assert inspection.rounds[0].evaluation is not None
    assert inspection.rounds[1].evaluation is not None
    assert inspection.rounds[2].evaluation is not None


def test_target_replacement_preview_excludes_active_position_correction():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(
        section_key,
    )
    _commit_job(runtime,section_key)

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )

    resolution = _current_resolution(runtime,section_key)
    job = runtime.prepare_section_cgh(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert inspection.working_round is not None
    display = inspection.working_round.target_display

    assert "position_correction_active" not in resolution.details
    assert "round_index" not in resolution.details
    assert job.prepared_request.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
    assert "position_correction_active" not in job.resolution.details
    assert "round_index" not in job.resolution.details
    np.testing.assert_array_equal(
        resolution.spot_positions_kxy,
        job.resolution.spot_positions_kxy,
    )
    assert display is not None
    np.testing.assert_array_equal(
        display.positions_kxy,
        resolution.spot_positions_kxy,
    )


def test_active_position_correction_is_passed_to_vector_target_update():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)

    inspection = runtime.get_section_cgh_session_inspection(section_key)
    correction = inspection.position_correction
    assert correction is not None
    assert inspection.working_round is not None
    display = inspection.working_round.target_display
    assert display is not None

    job = runtime.prepare_section_cgh(section_key)

    assert job.resolution.target_array is None
    np.testing.assert_array_equal(
        display.positions_kxy,
        correction.corrected_positions_kxy,
    )
    np.testing.assert_array_equal(
        job.resolution.spot_positions_kxy,
        correction.corrected_positions_kxy,
    )
    assert "position_correction_active" not in job.resolution.details
    assert "round_index" not in job.resolution.details


def test_incompatible_restored_position_correction_is_rejected():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)

    config = runtime.create_config()
    section_config = config.sections[section_key]
    snapshot = section_config.cgh_session
    correction = snapshot.position_correction
    bad_correction = replace(
        correction,
        ideal_positions_kxy=correction.ideal_positions_kxy + 1e-3,
    )
    bad_snapshot = replace(snapshot,position_correction=bad_correction)
    bad_sections = dict(config.sections)
    bad_sections[section_key] = replace(
        section_config,
        cgh_session=bad_snapshot,
    )
    bad_config = replace(config,sections=bad_sections)

    try:
        SLMRuntime.from_config(bad_config,registries=DEFAULT_REGISTRIES)
    except RuntimeError as error:
        assert "position correction" in str(error).lower()
    else:
        raise AssertionError("Expected incompatible position correction rejection")


def test_incompatible_restored_committed_target_signature_is_rejected():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    config = runtime.create_config()
    section_config = config.sections[section_key]
    snapshot = section_config.cgh_session
    bad_target = replace(
        snapshot.committed_target,
        target_signature=CGHSignature("bad-target-signature"),
    )
    bad_snapshot = replace(snapshot,committed_target=bad_target)
    bad_sections = dict(config.sections)
    bad_sections[section_key] = replace(
        section_config,
        cgh_session=bad_snapshot,
    )
    bad_config = replace(config,sections=bad_sections)

    try:
        SLMRuntime.from_config(bad_config,registries=DEFAULT_REGISTRIES)
    except RuntimeError as error:
        assert "target signature" in str(error).lower()
    else:
        raise AssertionError("Expected target signature restore rejection")


def test_position_reset_and_target_replacement_clear_localization_reference():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    assert runtime.get_section_feedback_status(
        section_key,
    ).previous_localization_available

    runtime.apply_section_position_correction(
        section_key,
    )

    assert not runtime.get_section_feedback_status(
        section_key,
    ).previous_localization_available

    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )
    _commit_job(runtime,section_key)

    assert not runtime.get_section_feedback_status(
        section_key,
    ).previous_localization_available


def test_clear_cgh_session_clears_persisted_session():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    assert runtime.clear_section_cgh_session(section_key)

    status = runtime.get_section_cgh_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.MISSING
    assert inspection.committed_target is None
    assert inspection.rounds == ()
    assert runtime.create_config().sections[section_key].cgh_session is None

    restored = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )
    assert restored.get_section_cgh_result_copy(section_key) is None


def test_pending_adaptation_keeps_source_round_editable_and_is_discarded_by_new_measurement():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)

    assert runtime.get_section_feedback_status(section_key).adaptation_pending

    replacement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="replacement",
    )
    runtime.set_section_feedback_measurement(section_key,replacement)

    status = runtime.get_section_cgh_status(section_key)
    feedback = runtime.get_section_feedback_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert not feedback.adaptation_pending
    assert inspection.working_round is None
    assert inspection.rounds[-1].evaluation is not None
    assert (
        inspection.rounds[-1].evaluation.measurement.acquisition.measurement_id
        == replacement.measurement_id
    )
    assert inspection.rounds[-1].evaluation.measurement.localization is None


def test_explicit_adapted_prepare_requires_and_targets_pending_adaptation():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)

    with np.testing.assert_raises_regex(RuntimeError,"No feedback adaptation"):
        runtime.prepare_section_adapted_cgh(section_key)

    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    job = runtime.prepare_section_adapted_cgh(section_key)

    assert job.prepared_request.purpose is CGHPreparedPurpose.WORKING_ROUND
    assert job.prepared_request.round_index == 1


def test_position_change_creates_pending_round_zero_and_adapted_prepare_accepts_it():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    transition = runtime.apply_section_position_correction(section_key)

    assert transition is not None
    feedback = runtime.get_section_feedback_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert feedback.position_active
    assert not feedback.adaptation_pending
    assert feedback.feedback_compute_pending
    assert feedback.pending_feedback_change is FeedbackChangeKind.POSITION
    assert inspection.rounds == ()
    assert inspection.working_round is not None
    assert inspection.working_round.index == 0

    job = runtime.prepare_section_adapted_cgh(section_key)
    assert job.prepared_request.purpose is CGHPreparedPurpose.WORKING_ROUND
    assert job.prepared_request.round_index == 0

    yy,xx = np.indices(job.spec.context.shape,dtype=np.float64)
    pattern = np.exp(1j*2*np.pi*(xx + yy)/(2*job.spec.context.shape[1]))
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=pattern,
    )
    assert runtime.commit_section_cgh(section_key,result) is not None

    feedback = runtime.get_section_feedback_status(section_key)
    status = runtime.get_section_cgh_status(section_key)
    assert status.result_state is CGHResultState.CURRENT
    assert not feedback.feedback_compute_pending
    assert feedback.pending_feedback_change is None


def test_position_disable_and_clear_remain_pending_even_when_not_active():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)
    _commit_job(runtime,section_key)

    runtime.set_section_position_correction_active(section_key,False)
    feedback = runtime.get_section_feedback_status(section_key)
    assert feedback.position_available
    assert not feedback.position_active
    assert feedback.feedback_compute_pending
    assert feedback.pending_feedback_change is FeedbackChangeKind.POSITION

    runtime.clear_section_position_correction(section_key)
    feedback = runtime.get_section_feedback_status(section_key)
    assert not feedback.position_available
    assert not feedback.position_active
    assert feedback.feedback_compute_pending
    assert feedback.pending_feedback_change is FeedbackChangeKind.POSITION


def test_explicit_base_prepare_preserves_feedback_until_success_then_restarts_round_zero():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    before = runtime.get_section_cgh_session_inspection(section_key)
    assert len(before.rounds) == 2

    job = runtime.prepare_section_base_cgh(section_key)
    during = runtime.get_section_cgh_session_inspection(section_key)
    assert job.prepared_request.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
    assert job.prepared_request.round_index == 0
    assert len(during.rounds) == 2

    yy,xx = np.indices(job.spec.context.shape,dtype=np.float64)
    pattern = np.exp(1j*2*np.pi*(xx + yy)/(2*job.spec.context.shape[1]))
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=pattern,
    )
    assert runtime.commit_section_cgh(section_key,result) is not None

    after = runtime.get_section_cgh_session_inspection(section_key)
    feedback = runtime.get_section_feedback_status(section_key)
    assert len(after.rounds) == 1
    assert after.rounds[0].index == 0
    assert feedback.intensity_count == 0
    assert not feedback.adaptation_pending


def test_intensity_analysis_is_authoritative_for_metrics_and_adaptation():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _measurement,_localization = _attach_localized_measurement(
        runtime,section_key,powers=(1.0,1.0,0.25,1.0),
    )

    before = runtime.get_section_cgh_session_inspection(section_key)
    analysis = before.rounds[0].evaluation.intensity_analysis
    assert analysis is not None
    status = runtime.get_section_feedback_status(section_key)
    assert status.measurement_metrics == dict(analysis.values)

    runtime.apply_section_intensity_feedback(section_key)
    after = runtime.get_section_cgh_session_inspection(section_key)
    assert after.rounds[0].evaluation.intensity_analysis is analysis
    assert after.working_round is not None
    measured = np.asarray(analysis.spot_powers,dtype=np.float64)
    expected = np.ones_like(measured)
    expected *= float(np.mean(measured)) / (measured + 1e-9)
    expected /= float(np.max(expected))
    np.testing.assert_allclose(after.working_round.intensities,expected)


def test_intensity_analysis_parameter_change_invalidates_pending_adaptation_on_round_zero():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    assert runtime.get_section_feedback_status(section_key).adaptation_pending

    assert runtime.update_section_feedback_parameters(
        section_key,"intensity_analysis",{"integration_size_px":7},
    )

    status = runtime.get_section_feedback_status(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert status.intensity_params["integration_size_px"] == 7
    assert not status.adaptation_pending
    assert inspection.working_round is None
    assert inspection.rounds[0].evaluation is not None
    assert inspection.rounds[0].evaluation.intensity_analysis is None

    analysis = runtime.compute_section_feedback_intensity_analysis(section_key)
    runtime.set_section_feedback_intensity_analysis(section_key,analysis)
    assert analysis.parameters["integration_size_px"] == 7


def test_intensity_analysis_parameters_lock_after_round_one_and_unlock_after_reset():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    assert runtime.update_section_feedback_parameters(
        section_key,"intensity_analysis",{"integration_size_px":7},
    )
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    _commit_job(runtime,section_key)

    status = runtime.get_section_feedback_status(section_key)
    assert status.intensity_count == 1
    assert status.intensity_params["integration_size_px"] == 7
    with pytest.raises(RuntimeError,match="locked after Round 1"):
        runtime.update_section_feedback_parameters(
            section_key,"intensity_analysis",{"integration_size_px":9},
        )
    assert (
        runtime.get_section_feedback_status(section_key)
        .intensity_params["integration_size_px"] == 7
    )

    runtime.reset_section_intensity_feedback(section_key)
    assert runtime.get_section_feedback_status(section_key).intensity_count == 0
    assert runtime.update_section_feedback_parameters(
        section_key,"intensity_analysis",{"integration_size_px":9},
    )
    assert (
        runtime.get_section_feedback_status(section_key)
        .intensity_params["integration_size_px"] == 9
    )


def test_intensity_analysis_parameters_roundtrip_with_session_snapshot():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    runtime.update_section_feedback_parameters(
        section_key,"intensity_analysis",{"integration_size_px":7},
    )
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    _commit_job(runtime,section_key)

    restored = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )
    status = restored.get_section_feedback_status(section_key)
    inspection = restored.get_section_cgh_session_inspection(section_key)
    assert status.intensity_params["integration_size_px"] == 7
    assert inspection.rounds[0].evaluation.intensity_analysis is not None
    assert (
        inspection.rounds[0].evaluation.intensity_analysis
        .parameters["integration_size_px"] == 7
    )


def test_session_inspection_exposes_current_and_pending_target_display_data():
    runtime,section_key = _runtime()
    job,_result = _commit_job(runtime,section_key)
    before = runtime.get_section_cgh_session_inspection(section_key)
    current_display = before.rounds[0].target_display
    assert before.rounds[0].target_preview is not None
    assert current_display is not None
    np.testing.assert_array_equal(
        current_display.positions_kxy,
        job.resolution.spot_positions_kxy,
    )
    np.testing.assert_array_equal(
        current_display.intensities,
        before.rounds[0].intensities,
    )
    assert not current_display.positions_kxy.flags.writeable
    assert not current_display.intensities.flags.writeable

    _attach_localized_measurement(
        runtime,section_key,powers=(1.0,1.0,0.25,1.0),
    )
    runtime.apply_section_intensity_feedback(section_key)
    pending = runtime.get_section_cgh_session_inspection(section_key)
    assert pending.working_round is not None
    pending_display = pending.working_round.target_display
    assert pending.working_round.target_preview is not None
    assert pending_display is not None
    assert not np.array_equal(
        pending.rounds[0].intensities,
        pending.working_round.intensities,
    )
    np.testing.assert_array_equal(
        pending_display.positions_kxy,
        current_display.positions_kxy,
    )
    np.testing.assert_array_equal(
        pending_display.intensities,
        pending.working_round.intensities,
    )


def test_session_inspection_target_display_uses_raster_target_resolution():
    runtime,section_key = _runtime()
    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","selected_target"):"multi_foci",
            ("cgh","multi_foci","params","n_foci_x"):2,
            ("cgh","multi_foci","params","n_foci_y"):2,
        },
    )
    job,_result = _commit_job(runtime,section_key)

    inspection = runtime.get_section_cgh_session_inspection(section_key)
    display = inspection.rounds[0].target_display

    assert display is not None
    np.testing.assert_array_equal(
        display.positions_kxy,
        job.resolution.spot_positions_kxy,
    )
    np.testing.assert_array_equal(
        display.intensities,
        job.resolution.spot_intensities,
    )


def test_position_correction_preserves_pre_correction_reference_round():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    measurement,localization = _attach_localized_measurement(runtime,section_key)
    before = runtime.get_section_cgh_session_inspection(section_key)
    source_result = before.rounds[0].result

    runtime.apply_section_position_correction(section_key)

    inspection = runtime.get_section_cgh_session_inspection(section_key)
    reference = inspection.position_reference_round
    assert reference is not None
    assert reference.result is source_result
    assert reference.evaluation is not None
    assert reference.evaluation.measurement is not None
    assert (
        reference.evaluation.measurement.acquisition.measurement_id
        == measurement.measurement_id
    )
    np.testing.assert_array_equal(
        reference.evaluation.measurement.acquisition.image,
        measurement.image,
    )
    assert reference.evaluation.measurement.localization is localization
    assert inspection.rounds == ()
    assert inspection.working_round is not None
    assert inspection.working_round.index == 0


def test_position_reference_survives_corrected_rounds_and_disable_but_clear_removes_it():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)
    reference = runtime.get_section_cgh_session_inspection(
        section_key
    ).position_reference_round
    assert reference is not None

    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert inspection.position_reference_round is not None
    assert inspection.position_reference_round.result is reference.result

    runtime.set_section_position_correction_active(
        section_key,False,reset_intensity=True,
    )
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert inspection.position_reference_round is not None

    runtime.clear_section_position_correction(section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert inspection.position_reference_round is None


def test_position_reference_round_roundtrips_with_source_measurement():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    measurement,_localization = _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)
    _commit_job(runtime,section_key)
    _commit_next_intensity_round(runtime,section_key)

    restored = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )
    inspection = restored.get_section_cgh_session_inspection(section_key)
    reference = inspection.position_reference_round
    assert reference is not None
    assert reference.result is not None
    assert reference.evaluation is not None
    assert reference.evaluation.measurement is not None
    acquisition = reference.evaluation.measurement.acquisition
    assert acquisition.measurement_id == measurement.measurement_id
    np.testing.assert_array_equal(acquisition.image,measurement.image)
    assert reference.evaluation.measurement.localization is not None
    assert [item.index for item in inspection.rounds] == [0,1]


def test_target_replacement_clears_position_reference_round():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)
    _commit_job(runtime,section_key)
    assert runtime.get_section_cgh_session_inspection(
        section_key
    ).position_reference_round is not None

    runtime.apply_section_patch(
        section_key,
        {("cgh","multi_foci_vector","params","n_foci_x"):3},
    )
    _commit_job(runtime,section_key)
    inspection = runtime.get_section_cgh_session_inspection(section_key)
    assert inspection.position_reference_round is None
    assert inspection.position_correction is None

from dataclasses import replace

import pytest

from slmcore.cgh.feedback import base_cgh_recompute_would_discard_feedback


def test_qt_interaction_classifies_only_cgh_target_edits():
    pytest.importorskip("qtpy")
    from slmcore.qt.application.interaction import ParameterEditKind,classify_parameter_edit

    assert classify_parameter_edit({
        ("cgh","selected_target"):"multi_foci",
    }) is ParameterEditKind.CGH_TARGET
    assert classify_parameter_edit({
        ("cgh","multi_foci","params","period_x_px"):12,
    }) is ParameterEditKind.CGH_TARGET
    assert classify_parameter_edit({
        ("cgh","multi_foci","params","square"):True,
        ("cgh","multi_foci","params","square_unit"):"slm",
    }) is ParameterEditKind.CGH_TARGET
    assert classify_parameter_edit({
        ("cgh","multi_foci","computation","iterations"):100,
    }) is ParameterEditKind.STANDARD
    assert classify_parameter_edit({
        ("optics","wavelength_nm"):510,
    }) is ParameterEditKind.STANDARD


def test_feedback_loss_predicate_matches_base_cgh_replacement_semantics():
    from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
    from slmcore.engine.section import split_slm_geometry

    geometry = SLMGeometry(width=12,height=6,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    status = runtime.get_section_feedback_status("sec_0")
    assert not base_cgh_recompute_would_discard_feedback(status)
    assert base_cgh_recompute_would_discard_feedback(
        replace(status,intensity_count=1)
    )
    assert base_cgh_recompute_would_discard_feedback(
        replace(status,position_available=True)
    )
    assert base_cgh_recompute_would_discard_feedback(
        replace(status,adaptation_pending=True)
    )
    assert base_cgh_recompute_would_discard_feedback(
        replace(status,feedback_compute_pending=True)
    )

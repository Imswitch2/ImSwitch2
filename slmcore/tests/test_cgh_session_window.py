from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.computations import CGHIterationMetrics
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.core.measurement import ImageMeasurement
from slmcore.core.engine.section import split_slm_geometry


def _runtime():
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


def _commit_job(runtime,section_key):
    job = runtime.prepare_section_cgh(section_key)
    yy,xx = np.indices(job.spec.context.shape,dtype=np.float64)
    pattern = np.exp(1j*2*np.pi*(xx + yy)/(2*job.spec.context.shape[1]))
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=pattern,
        metrics=(
            CGHIterationMetrics(
                iteration=1,efficiency=0.7,uniformity=0.8,normalized_std=0.1,
            ),
            CGHIterationMetrics(
                iteration=2,efficiency=0.75,uniformity=0.85,normalized_std=0.08,
            ),
        ),
    )
    assert runtime.commit_section_cgh(section_key,result) is not None
    return result


def _current_resolution(runtime,section_key):
    section = runtime._get_section(section_key)
    return section._cgh_session.create_target_resolution(
        section.state.cgh,
        section._build_context(section.state),
    )


def _attach_localized_measurement(
    runtime,section_key,powers=(1.0,1.0,1.0,1.0),
):
    resolution = _current_resolution(runtime,section_key)
    n_spots = int(resolution.lattice_indices.shape[1])
    image = np.zeros((64,64),dtype=np.float64)
    expected = np.array([
        [16.0,48.0,16.0,48.0],
        [16.0,16.0,48.0,48.0],
    ],dtype=np.float64)[:,:n_spots]
    for (x,y),power in zip(expected.T,tuple(powers)[:n_spots]):
        image[int(y),int(x)] = float(power)

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
        crop_coord=(0,64,0,64),
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


def _qapp_and_window_class():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.cgh.session_window import CGHSessionWindow
    except Exception as error:
        pytest.skip("CGHSessionWindow dependencies are unavailable: %s" % error)
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app,QtWidgets,CGHSessionWindow


def _summary(runtime,section_key):
    state = runtime.get_section_state_copy(section_key).cgh
    key = state.selected_target
    registration = DEFAULT_REGISTRIES.targets[key]
    target = state.items[key]
    summary = {
        "target_presentation":registration.presentation,
        "target_param_specs":registration.params,
        "target_params":dict(target.params.values),
        "algorithm":str(target.algorithm),
        "parameters":dict(target.computation.params.values),
        "unit_mode":"slm",
        "conversion_context":None,
    }
    result = runtime.get_section_cgh_result_copy(section_key)
    if result is not None:
        applied_registration = DEFAULT_REGISTRIES.targets[result.spec.target_type]
        summary.update({
            "applied_target_presentation":applied_registration.presentation,
            "applied_target_param_specs":applied_registration.params,
            "applied_target_params":dict(result.spec.target_params),
            "applied_unit_mode":"slm",
            "applied_conversion_context":None,
        })
    return summary


def _window(runtime,section_key):
    _app,QtWidgets,CGHSessionWindow = _qapp_and_window_class()
    return QtWidgets,CGHSessionWindow(
        status=runtime.get_section_feedback_status(section_key),
        inspection=runtime.get_section_feedback_inspection(section_key),
        session_inspection=runtime.get_section_cgh_session_inspection(section_key),
        cgh_status=runtime.get_section_cgh_status(section_key),
        localization_context=None,
        detectors=("Camera",),
        current_detector="Camera",
        cgh_summary=_summary(runtime,section_key),
    )



def test_cgh_group_keeps_target_in_header_and_status_in_session_dashboard():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _app,QtWidgets,_CGHSessionWindow = _qapp_and_window_class()
    from slmcore.qt.sections.view import SectionView

    view = SectionView(
        section_key=section_key,
        snapshot=runtime.get_section_snapshot(section_key),
    )
    try:
        view.set_cgh_target_presentation(_summary(runtime,section_key))
        view.apply_feedback_status(
            runtime.get_section_feedback_status(section_key)
        )
        cgh = view.groups["cgh"]
        header_summary = cgh.param_section.summary_label.full_text()
        assert "Multi Foci" in header_summary
        assert "2×2" in header_summary
        assert "Px" in header_summary and "Py" in header_summary
        assert "Current" not in header_summary
        assert cgh.status_label is None
        assert cgh._feedback_controls.status_value_label.text() == "Current"
        assert "Intensity ×0" in cgh._feedback_controls.feedback_summary_label.text()
        assert "Position" not in cgh._feedback_controls.feedback_summary_label.text()
        assert cgh._feedback_controls.position_checkbox.text() == "Position"
        assert not cgh._feedback_controls.position_checkbox.isEnabled()
        selector = cgh.param_section.metadata_field("selected_target")
        assert selector.label.text() == "Selected Target:"
        assert cgh.restore_target_button.text() == "Restore"
        assert cgh.compute_button.text() == "Recompute"
        assert "already current" in cgh.compute_button.toolTip()
        assert cgh._feedback_controls.open_button.text() == "Open session"
        assert cgh.clear_button.text() == "Clear session"
        assert cgh.preview_button.text() == "Visualize Target"
        header_grid = cgh._feedback_controls.container.layout()
        assert header_grid.columnStretch(1) == 0
        assert header_grid.columnStretch(4) == 1
        assert header_grid.indexOf(selector.editor) == -1
        assert cgh._action_widget is None

        actions = []
        cgh.set_action_handler(
            lambda action,options: actions.append((action,options))
        )

        cgh.compute_button.click()
        cgh._feedback_controls.open_button.click()
        cgh.clear_button.click()
        assert actions == [
            ("compute",{}),
            ("open_measurements_corrections",{}),
            ("clear_cgh_session",{}),
        ]

        from dataclasses import replace
        feedback_status = runtime.get_section_feedback_status(section_key)
        view.apply_feedback_status(replace(
            feedback_status,position_available=True,position_active=False,
            position_stale=False,intensity_count=0,
        ))
        position_checkbox = cgh._feedback_controls.position_checkbox
        assert position_checkbox.isEnabled()
        position_checkbox.click()
        assert actions[-1] == ("position_set_active",{"active":True})

        view.apply_feedback_status(replace(
            feedback_status,position_available=True,position_active=True,
            position_stale=False,intensity_count=1,
        ))
        assert position_checkbox.isChecked()
        assert not position_checkbox.isEnabled()
        assert "Reset intensity feedback" in position_checkbox.toolTip()

        runtime.apply_section_patch(
            section_key,{
                ("cgh","multi_foci_vector","params","n_foci_x"):3,
            },
        )
        view.apply_cgh_status(runtime.get_section_cgh_status(section_key))
        assert cgh.compute_button.text() == "Compute"
        assert cgh.compute_button.toolTip() == ""
        cgh.set_computing(True)
        assert cgh.compute_button.text() == "Computing..."
        cgh.set_computing(False)
        assert cgh.compute_button.text() == "Compute"
    finally:
        view.deleteLater()


def test_cgh_group_collapsed_summary_uses_applied_not_selected_target():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _app,_QtWidgets,_CGHSessionWindow = _qapp_and_window_class()
    from slmcore.qt.sections.view import SectionView
    from slmcore.core.engine.registry import TargetPresentation

    view = SectionView(
        section_key=section_key,
        snapshot=runtime.get_section_snapshot(section_key),
    )
    try:
        summary = dict(_summary(runtime,section_key))
        summary["target_presentation"] = TargetPresentation("Draft Target")
        summary["target_param_specs"] = {}
        summary["target_params"] = {}
        summary["applied_target_presentation"] = TargetPresentation("Applied Target")
        view.set_cgh_target_presentation(summary)
        cgh = view.groups["cgh"]
        assert cgh.param_section.summary_label.full_text() == "Applied Target"
        selector = cgh.param_section.metadata_field("selected_target")
        assert selector.label.text() == "Selected Target:"
        assert not hasattr(cgh._feedback_controls,"selected_target_label")
    finally:
        view.deleteLater()


def test_cgh_session_window_uses_global_round_selector_and_inspect_tab():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    QtWidgets,window = _window(runtime,section_key)
    try:
        assert [
            window.workspace_tabs.tabText(index)
            for index in range(window.workspace_tabs.count())
        ] == ["Measure & Correct","Inspect"]
        assert window.round_selector.count() == 1
        assert window.round_selector.currentData() == "round:0"
        assert window.round_selector.currentText() == "Round 0"
        assert "Multi Foci" in window.target_summary_label.text()
        assert "multi_foci" not in window.target_summary_label.text().lower()
        assert not any(
            label.text() == "View:"
            for label in window.findChildren(QtWidgets.QLabel)
        )
        image_combo = window.measurement_view._workbench.result_view.image_mode_combo
        assert not image_combo.isHidden()
        assert image_combo.count() == 1
        assert image_combo.currentText() == "Localization"
        result_extras = window.measurement_view._workbench.result_view.result_extras
        assert result_extras.isAncestorOf(window.intensity_form.fields[
            "integration_size_px"
        ].editor)
        assert window.inspect_view.propagation_view.graphics.minimumHeight() >= 440
    finally:
        window.close()
        window.deleteLater()


def test_pending_adaptation_stays_on_source_round_until_compute_succeeds():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)

    _QtWidgets,window = _window(runtime,section_key)
    try:
        assert window.round_selector.count() == 1
        assert window.round_selector.currentData() == "round:0"
        assert window.round_selector.currentText() == "Round 0"
        controls = window.measurement_view._workbench.measurement_controls
        assert controls.acquire_button.isEnabled()
        assert controls.load_button.isEnabled()
        assert (
            controls.status_label.full_text()
            == "Adaptation pending · source round remains editable"
        )
        assert (
            controls.status_label.toolTip()
            == "Adaptation pending · source round remains editable"
        )
        assert window._compute_adapted_buttons[0].isEnabled()
        assert window.inspect_view.propagation_view.simulate_button.isEnabled()

        _commit_job(runtime,section_key)
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert window.round_selector.count() == 2
        assert window.round_selector.currentData() == "round:1"
        assert window.round_selector.currentText() == "Round 1"
    finally:
        window.close()
        window.deleteLater()


def test_historical_round_is_read_only_and_can_request_reset():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    _commit_job(runtime,section_key)

    _QtWidgets,window = _window(runtime,section_key)
    actions = []
    window.sigActionRequested.connect(
        lambda action,options:actions.append((action,dict(options)))
    )
    try:
        index = window.round_selector.findData("round:0")
        assert index >= 0
        window.round_selector.setCurrentIndex(index)
        assert not window.reset_round_button.isHidden()
        controls = window.measurement_view._workbench.measurement_controls
        assert not controls.acquire_button.isEnabled()
        assert controls.status_label.full_text() == "Historical round · read only"

        window.reset_round_button.click()
        assert actions[-1] == ("reset_to_round",{"round_index":0})
    finally:
        window.close()
        window.deleteLater()


def test_inspect_propagation_emits_selected_round_and_pad_size():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    actions = []
    window.sigActionRequested.connect(
        lambda action,options:actions.append((action,dict(options)))
    )
    try:
        propagation = window.inspect_view.propagation_view
        propagation.pad_size.setValue(1536)
        propagation.simulate_button.click()
        assert actions[-1] == (
            "propagate_selected",{
                "position_context":"corrected",
                "round_index":0,
                "pad_size":1536,
            },
        )
    finally:
        window.close()
        window.deleteLater()


def test_intensity_analysis_control_is_left_side_and_locks_after_round_one():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)

    _QtWidgets,window = _window(runtime,section_key)
    try:
        editor = window.intensity_form.fields["integration_size_px"].editor
        assert editor.isEnabled()
        assert window.measurement_uniformity_label.text() != "—"
        assert window.measurement_efficiency_label.text() != "—"

        runtime.apply_section_intensity_feedback(section_key)
        _commit_job(runtime,section_key)
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert not editor.isEnabled()
        assert "Locked after Round 1" in editor.toolTip()

        runtime.reset_section_intensity_feedback(section_key)
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert editor.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_pending_adaptation_view_shows_pending_target_and_round_metrics_history():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(
        runtime,section_key,powers=(1.0,1.0,0.25,1.0),
    )
    runtime.apply_section_intensity_feedback(section_key)

    _QtWidgets,window = _window(runtime,section_key)
    try:
        pending = runtime.get_section_cgh_session_inspection(section_key)
        assert pending.working_round is not None
        pending_display = pending.working_round.target_display
        assert pending_display is not None
        np.testing.assert_array_equal(
            window.feedback_target_view._positions_kxy,
            pending_display.positions_kxy,
        )
        np.testing.assert_array_equal(
            window.feedback_target_view._intensities,
            pending_display.intensities,
        )
        assert window.feedback_target_view.status_label.text() == "Pending adaptation"
        assert len(window.metrics_history_view.plot.listDataItems()) == 2

        _commit_job(runtime,section_key)
        _attach_localized_measurement(
            runtime,section_key,powers=(1.0,0.9,1.0,0.8),
        )
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert window.feedback_target_view.status_label.text() == "Current round target"
        data_items = window.metrics_history_view.plot.listDataItems()
        assert len(data_items) == 2
        assert all(len(item.xData) == 2 for item in data_items)
    finally:
        window.close()
        window.deleteLater()


def test_cgh_computing_refreshes_interactivity_without_round_combo_roundtrip():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        controls = window.measurement_view._workbench.measurement_controls
        assert controls.acquire_button.isEnabled()
        window.set_cgh_computing(True)
        assert not controls.acquire_button.isEnabled()
        window.set_cgh_computing(False)
        assert controls.acquire_button.isEnabled()
        assert window.round_selector.currentData() == "round:0"
    finally:
        window.close()
        window.deleteLater()


def test_propagation_cache_follows_round_generation_and_pad_size():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_intensity_feedback(section_key)
    _commit_job(runtime,section_key)

    _QtWidgets,window = _window(runtime,section_key)
    try:
        round0 = window.round_selector.findData("round:0")
        assert round0 >= 0
        window.round_selector.setCurrentIndex(round0)
        propagation = window.inspect_view.propagation_view
        propagation.pad_size.setValue(1024)
        propagation.simulate_button.click()
        image = np.arange(16,dtype=np.float64).reshape(4,4)
        window.set_propagation_result(0,image)
        assert np.array_equal(propagation._image,image)

        round1 = window.round_selector.findData("round:1")
        window.round_selector.setCurrentIndex(round1)
        assert propagation._image is None
        assert propagation.status_label.text() == "Not simulated for this round."

        window.round_selector.setCurrentIndex(round0)
        assert np.array_equal(propagation._image,image)

        propagation.pad_size.setValue(1536)
        assert propagation._image is None
        propagation.pad_size.setValue(1024)
        assert np.array_equal(propagation._image,image)
    finally:
        window.close()
        window.deleteLater()


def test_intensity_preview_toggle_and_reuse_policy_follow_round_state():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        result_view = window.measurement_view._workbench.result_view
        image_combo = result_view.image_mode_combo
        assert image_combo.findData("Integration") >= 0
        assert image_combo.isEnabled()
        assert window.reuse_localization_checkbox.isChecked()
        assert window.reuse_localization_checkbox.isEnabled()

        result_view.show_detected.setChecked(False)
        image_combo.setCurrentIndex(image_combo.findData("Integration"))
        assert not result_view.show_detected.isEnabled()
        assert not result_view.show_expected.isEnabled()
        assert not result_view.detected_item.isVisible()
        assert not result_view.expected_item.isVisible()

        image_combo.setCurrentIndex(image_combo.findData("Localization"))
        assert result_view.show_detected.isEnabled()
        assert result_view.show_expected.isEnabled()
        assert not result_view.show_detected.isChecked()
        assert result_view.expected_item.isVisible()

        localization_splitter = window.measurement_view._workbench.vertical_splitter
        assert localization_splitter.childrenCollapsible()
        assert localization_splitter.isCollapsible(1)
        assert window.intensity_splitter.childrenCollapsible()
        assert window.intensity_splitter.isCollapsible(1)

        runtime.apply_section_intensity_feedback(section_key)
        _commit_job(runtime,section_key)
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert window.reuse_localization_checkbox.isEnabled()
        assert window.reuse_localization_checkbox.isChecked()
    finally:
        window.close()
        window.deleteLater()



def test_session_header_and_intensity_controls_use_compact_layout():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        assert not hasattr(window,"session_label")
        assert not hasattr(window,"load_session_button")
        assert "Multi Foci" in window.target_summary_label.text()
        assert window.round_selector.currentText() == "Round 0"
        assert window.reuse_localization_checkbox.parentWidget() is not None
        assert window.metrics_history_view.parentWidget() is not None

        top = window.intensity_splitter.widget(0)
        history = window.intensity_splitter.widget(1)
        assert top is not None
        assert history is not None
        assert history.title() == "Metrics history"
    finally:
        window.close()
        window.deleteLater()

def test_performance_graph_uses_distinct_metric_pens_and_compact_height():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        performance = window.inspect_view.performance_view
        assert performance.plot.maximumHeight() <= 280
        items = performance.plot.listDataItems()
        assert len(items) == 3
        colors = {
            item.opts["pen"].color().name()
            for item in items
        }
        assert len(colors) == 3
    finally:
        window.close()
        window.deleteLater()


def test_cgh_session_embedded_graphics_have_explicit_widget_parents():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    QtWidgets,window = _window(runtime,section_key)
    try:
        localization = window.measurement_view._workbench.result_view
        assert localization.graphics.parentWidget() is localization

        target = window.feedback_target_view
        assert target.graphics.parentWidget() is target

        history = window.metrics_history_view
        assert history.plot.parentWidget() is history

        performance = window.inspect_view.performance_view
        assert performance.plot.parentWidget() is performance

        propagation = window.inspect_view.propagation_view
        assert propagation.graphics.parentWidget() is propagation
    finally:
        window.close()
        window.deleteLater()


def test_localization_parameter_scrolls_stay_compact_but_expandable():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    QtWidgets,window = _window(runtime,section_key)
    try:
        workbench = window.measurement_view._workbench
        scrolls = [
            workbench.geometry_group.findChild(QtWidgets.QScrollArea),
            workbench.options_group.findChild(QtWidgets.QScrollArea),
        ]
        assert all(scroll is not None for scroll in scrolls)
        assert all(scroll.minimumHeight() == 60 for scroll in scrolls)
        assert all(
            scroll.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Ignored
            for scroll in scrolls
        )
        assert all(
            group.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Ignored
            for group in (workbench.geometry_group,workbench.options_group)
        )
    finally:
        window.close()
        window.deleteLater()


def test_localization_lower_pane_matches_metrics_history_height():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    QtWidgets,window = _window(runtime,section_key)
    try:
        right_sizes = window.intensity_splitter.sizes()
        assert len(right_sizes) == 2
        window.measurement_view.set_lower_pane_height(right_sizes[1])
        left_sizes = window.measurement_view._workbench.vertical_splitter.sizes()
        assert abs(left_sizes[1]-right_sizes[1]) <= 1
    finally:
        window.close()
        window.deleteLater()


def test_position_tab_uses_right_controls_and_pending_position_is_actionable():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    runtime.apply_section_position_correction(section_key)

    QtWidgets,window = _window(runtime,section_key)
    try:
        position_tab = window.feedback_tabs.widget(1)
        assert isinstance(position_tab.layout(),QtWidgets.QHBoxLayout)
        assert position_tab.layout().count() == 2

        controls_parent = window.position_apply_button.parentWidget()
        assert controls_parent is window.position_clear_button.parentWidget()
        assert not hasattr(window,"position_toggle_button")
        assert controls_parent.minimumWidth() == 175
        assert controls_parent.maximumWidth() == 220

        assert (
            window.position_status_label.text()
            == "Applying correction · hologram pending"
        )
        assert window.position_use_checkbox.isChecked()
        assert not window.position_use_checkbox.isEnabled()
        assert not window.position_apply_button.isEnabled()
        assert window._compute_adapted_buttons[1].isEnabled()
        assert not window.position_clear_button.isEnabled()

        legend_texts = [
            label.text()
            for label in window.position_correction_view.findChildren(
                QtWidgets.QLabel
            )
        ]
        assert "Color = |Δk|" in legend_texts
        assert any(text.startswith("Vector: ideal") for text in legend_texts)
        assert window.position_correction_view.vector_scale.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_position_history_selector_appears_only_after_position_correction():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    QtWidgets,window = _window(runtime,section_key)
    try:
        assert window.position_selector.isHidden()
        assert window.position_selector_label.isHidden()

        _attach_localized_measurement(runtime,section_key)
        runtime.apply_section_intensity_feedback(section_key)
        _commit_job(runtime,section_key)
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            None,
            _summary(runtime,section_key),
        )
        assert window.position_selector.isHidden()
    finally:
        window.close()
        window.deleteLater()


def test_position_history_selector_separates_reference_from_intensity_rounds():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    source = runtime.get_section_cgh_session_inspection(section_key).rounds[-1]
    measurement = source.evaluation.measurement.acquisition
    runtime.apply_section_position_correction(section_key)

    QtWidgets,window = _window(runtime,section_key)
    try:
        assert not window.position_selector.isHidden()
        assert window.position_selector.currentText() == "Corrected"
        assert window.round_selector.isEnabled()
        assert window.round_selector.currentData() == "working:0"

        reference_index = window.position_selector.findData("not_corrected")
        assert reference_index >= 0
        window.position_selector.setCurrentIndex(reference_index)

        assert window.position_selector.currentText() == "Not Corrected"
        assert not window.round_selector.isEnabled()
        assert window.round_selector.currentText() == "—"
        assert window.reset_round_button.isHidden()
        assert window.measurement_view.measurement is not None
        assert (
            window.measurement_view.measurement.measurement_id
            == measurement.measurement_id
        )
        assert (
            window.measurement_view._workbench.measurement_controls.status_label.full_text()
            == "Position reference · read only"
        )
        propagation = window.inspect_view.propagation_view
        assert propagation.simulate_button.isEnabled()

        actions = []
        window.sigActionRequested.connect(
            lambda action,options:actions.append((action,dict(options)))
        )
        propagation.pad_size.setValue(1536)
        propagation.simulate_button.click()
        assert actions[-1] == (
            "propagate_selected",{
                "position_context":"not_corrected",
                "round_index":source.index,
                "pad_size":1536,
            },
        )

        corrected_index = window.position_selector.findData("corrected")
        assert corrected_index >= 0
        window.position_selector.setCurrentIndex(corrected_index)
        assert window.round_selector.isEnabled()
        assert window.round_selector.currentData() == "working:0"
    finally:
        window.close()
        window.deleteLater()


def test_feedback_workspace_geometry_is_warning_only():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        assert window.feedback_tabs.count() == 2
        assert window.feedback_tabs.tabText(0) == "Intensity"
        assert window.feedback_tabs.tabText(1) == "Position"

        window.set_geometry_calibration_status(
            observation_plane_name="sample",calibrated=False,
        )
        for warning in (
            window.intensity_geometry_warning,window.position_geometry_warning,
        ):
            assert not warning.isHidden()
            assert "Geometry not calibrated for sample" in warning.text()
            assert "Calibration → Geometry" in warning.text()
            assert "href" not in warning.text()

        window.set_geometry_calibration_status(
            observation_plane_name="sample",calibrated=True,
        )
        assert window.intensity_geometry_warning.isHidden()
        assert window.position_geometry_warning.isHidden()
    finally:
        window.close()
        window.deleteLater()

def test_session_window_observation_selector_is_explicit_and_emits_selection():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _app,QtWidgets,CGHSessionWindow = _qapp_and_window_class()
    window = CGHSessionWindow(
        status=runtime.get_section_feedback_status(section_key),
        inspection=runtime.get_section_feedback_inspection(section_key),
        session_inspection=runtime.get_section_cgh_session_inspection(section_key),
        cgh_status=runtime.get_section_cgh_status(section_key),
        localization_context=None,
        detectors=("MainCam","IntermediateCam"),
        current_detector="MainCam",
        observation_planes=("Main","Intermediate"),
        current_observation_plane=None,
        cgh_summary=_summary(runtime,section_key),
    )
    emitted = []
    window.sigActionRequested.connect(lambda action,options:emitted.append((action,options)))
    try:
        assert window.observation_plane_combo.currentData() is None
        index = window.observation_plane_combo.findData("Intermediate")
        assert index >= 0
        window.observation_plane_combo.setCurrentIndex(index)
        assert emitted[-1][0] == "observation_plane_set"
        assert emitted[-1][1]["plane"] == "Intermediate"
    finally:
        window.close()
        window.deleteLater()


def _test_position_preview(cropped_image, *, crop_offset=(0.0, 0.0)):
    image = np.asarray(cropped_image, dtype=np.float64)
    positions = np.array(
        [[1.0, max(1.0, image.shape[1] - 2.0)],
         [1.0, max(1.0, image.shape[0] - 2.0)]],
        dtype=np.float64,
    )
    center = np.mean(positions, axis=1)
    return {
        "cropped_image": image,
        "crop_offset_px": np.asarray(crop_offset, dtype=np.float64),
        "measured_positions_px": positions,
        "global_positions_px": positions,
        "reference_positions_px": positions,
        "raw_reference_positions_px": positions,
        "fit_center_px": center,
        "reference_center_px": center,
        "baseline_positions_kxy": np.zeros_like(positions),
        "ideal_positions_kxy": np.zeros_like(positions),
        "displacement_kxy": np.zeros_like(positions),
        "correction_mode": "per_spot",
        "polynomial_degree": 2,
        "affine_transform": None,
        "fitted_transform": None,
    }


def test_position_background_selector_keeps_localization_image_as_default():
    runtime, section_key = _runtime()
    _commit_job(runtime, section_key)
    _attach_localized_measurement(runtime, section_key)
    _QtWidgets, window = _window(runtime, section_key)
    try:
        localization_image = np.arange(20, dtype=np.float64).reshape(4, 5)
        preview = _test_position_preview(
            localization_image, crop_offset=(3.0, 2.0),
        )
        window.set_position_reference_state({
            "mode": "current",
            "reference_name": None,
            "references": (),
            "change_allowed": True,
            "correction_mode": "per_spot",
        }, preview)

        assert window.position_background_combo.currentData() == "localization"
        assert window.position_background_combo.findData("visual_snap") < 0

        full_image = np.arange(120, dtype=np.float64).reshape(10, 12)
        visual = ImageMeasurement(
            image=full_image, source="test", detector="Camera",
        )
        window.set_position_visual_background(visual)

        # Capturing a visual image must not silently replace the immutable
        # localization-image default.
        assert window.position_background_combo.currentData() == "localization"
        visual_index = window.position_background_combo.findData("visual_snap")
        assert visual_index >= 0
        expected_crop = full_image[2:6, 3:8]
        assert np.array_equal(window._visual_background_crop(), expected_crop)

        window.position_background_combo.setCurrentIndex(visual_index)
        assert window._position_background_mode == "visual_snap"
        assert np.array_equal(
            np.asarray(window.position_reference_view.image_item.image),
            expected_crop,
        )

        localization_index = window.position_background_combo.findData("localization")
        window.position_background_combo.setCurrentIndex(localization_index)
        assert np.array_equal(
            np.asarray(window.position_reference_view.image_item.image),
            localization_image,
        )
    finally:
        window.close()
        window.deleteLater()


def test_position_background_selector_is_only_shown_for_detector_overlay():
    runtime, section_key = _runtime()
    _commit_job(runtime, section_key)
    _QtWidgets, window = _window(runtime, section_key)
    try:
        detector_index = window.position_view_combo.findData("detector")
        correction_index = window.position_view_combo.findData("kxy")
        window.position_view_combo.setCurrentIndex(detector_index)
        assert not window.position_background_label.isHidden()
        assert not window.position_background_combo.isHidden()

        window.position_view_combo.setCurrentIndex(correction_index)
        assert window.position_background_label.isHidden()
        assert window.position_background_combo.isHidden()
    finally:
        window.close()
        window.deleteLater()


def test_visual_snap_action_is_capture_only_position_background_request():
    runtime, section_key = _runtime()
    _commit_job(runtime, section_key)
    _attach_localized_measurement(runtime, section_key)
    _QtWidgets, window = _window(runtime, section_key)
    emitted = []
    window.sigActionRequested.connect(
        lambda action, options: emitted.append((action, dict(options)))
    )
    try:
        preview = _test_position_preview(
            np.zeros((8, 8), dtype=np.float64), crop_offset=(0.0, 0.0),
        )
        window.set_position_reference_state({
            "mode": "current",
            "reference_name": None,
            "references": (),
            "change_allowed": True,
            "correction_mode": "per_spot",
        }, preview)
        assert window.position_visual_snap_button.isEnabled()

        window.position_visual_snap_button.click()
        assert emitted[-1] == (
            "visual_background_acquire", {"detector": "Camera"},
        )
    finally:
        window.close()
        window.deleteLater()


def test_position_correction_edit_controls_are_compact_and_report_edit_state():
    runtime,section_key=_runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window=_window(runtime,section_key)
    try:
        preview=_test_position_preview(np.zeros((8,8),dtype=np.float64))
        preview["reference"]={"mode":"current"}
        context={
            "mode":"current",
            "reference_name":None,
            "references":(),
            "change_allowed":True,
            "correction_mode":"affine",
            "polynomial_degree":2,
            "manual_transform":None,
        }
        window.set_position_reference_state(context,preview)
        assert window.position_reference_interactive_edit_checkbox.text()=="Edit"
        assert window.position_reference_reset_button.text()=="Reset"
        assert window.position_reference_edit_state_label.text()=="Automatic"

        context["manual_transform"]={
            "model":"affine",
            "matrix":((1.0,0.0,1.0),(0.0,1.0,0.0)),
        }
        window.set_position_reference_state(context,preview)
        assert window.position_reference_edit_state_label.text()=="Edited"
        assert window.position_reference_reset_button.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_current_free_edit_disables_per_spot_and_enters_with_translation():
    runtime,section_key=_runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window=_window(runtime,section_key)
    emitted=[]
    window.sigActionRequested.connect(
        lambda action,options:emitted.append((action,dict(options)))
    )
    try:
        preview=_test_position_preview(np.zeros((8,8),dtype=np.float64))
        window.set_position_reference_state({
            "mode":"global_fit",
            "reference_name":None,
            "references":(),
            "change_allowed":True,
            "correction_mode":"per_spot",
            "polynomial_degree":2,
            "manual_transform":None,
        },preview)

        current_index=window.position_reference_combo.findData("current")
        assert current_index>=0
        window.position_reference_combo.setCurrentIndex(current_index)
        assert emitted[-1][0]=="position_reference_set"
        assert emitted[-1][1]["mode"]=="current"
        assert emitted[-1][1]["correction_mode"]=="translation"

        window.set_position_reference_state({
            "mode":"current",
            "reference_name":None,
            "references":(),
            "change_allowed":True,
            "correction_mode":"translation",
            "polynomial_degree":2,
            "manual_transform":None,
        },preview)
        per_spot_index=window.position_reference_correction_model_combo.findData(
            "per_spot"
        )
        item=window.position_reference_correction_model_combo.model().item(
            per_spot_index
        )
        assert item is not None
        assert not item.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_current_free_edit_summary_reports_displacement_not_residual():
    runtime,section_key=_runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window=_window(runtime,section_key)
    try:
        preview=_test_position_preview(np.zeros((8,8),dtype=np.float64))
        preview["reference"]={"mode":"current"}
        preview["reference_positions_px"]=(
            np.asarray(preview["measured_positions_px"],dtype=np.float64)
            + np.asarray([[1.0],[0.0]])
        )
        window.set_position_reference_state({
            "mode":"current",
            "reference_name":None,
            "references":(),
            "change_allowed":True,
            "correction_mode":"translation",
            "polynomial_degree":2,
            "manual_transform":None,
        },preview)
        text=window.position_reference_view.summary_label.text()
        assert "displacement median" in text
        assert "displacement RMS" in text
        assert "residual" not in text
    finally:
        window.close()
        window.deleteLater()


def test_feedback_state_does_not_redraw_unchanged_committed_localization():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        committed=window.measurement_view.committed_result
        assert committed is not None
        calls=[]
        original=window.measurement_view.set_committed_result
        def counted(*args,**kwargs):
            calls.append(args[0])
            return original(*args,**kwargs)
        window.measurement_view.set_committed_result=counted
        window.set_feedback_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            None,
        )
        assert calls == []
        assert window.measurement_view.committed_result is committed
    finally:
        window.close()
        window.deleteLater()


def test_metrics_refresh_reuses_unchanged_integration_preview():
    from types import SimpleNamespace

    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        preview=np.arange(20,dtype=np.float64).reshape(4,5)
        metrics=SimpleNamespace(values={},integration_preview=preview)
        calls=[]
        original=window.measurement_view.set_auxiliary_image
        def counted(name,image):
            calls.append((name,image))
            return original(name,image)
        window.measurement_view.set_auxiliary_image=counted
        window._refresh_metrics_display(metrics)
        window._refresh_metrics_display(metrics)
        assert len(calls) == 1
        assert calls[0][0] == "Integration"
        assert calls[0][1] is preview
    finally:
        window.close()
        window.deleteLater()


def test_reference_refresh_does_not_reapply_localization_background():
    runtime,section_key = _runtime()
    _commit_job(runtime,section_key)
    _attach_localized_measurement(runtime,section_key)
    _QtWidgets,window = _window(runtime,section_key)
    try:
        preview=_test_position_preview(np.arange(20,dtype=np.float64).reshape(4,5))
        calls=[]
        original=window._apply_position_background_image
        def counted():
            calls.append(True)
            return original()
        window._apply_position_background_image=counted
        window.set_position_reference_state({
            "mode":"current",
            "reference_name":None,
            "references":(),
            "change_allowed":True,
            "correction_mode":"per_spot",
        },preview,lightweight=True)
        assert calls == []
    finally:
        window.close()
        window.deleteLater()

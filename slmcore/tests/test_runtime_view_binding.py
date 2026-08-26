import time

import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.engine.section import split_slm_geometry


def _app():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
    except Exception as error:
        pytest.skip(f"Qt bindings are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def _runtime(n_sections=2) -> SLMRuntime:
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,n_sections),
        registries=DEFAULT_REGISTRIES,
    )


def _process_for(app,milliseconds: int) -> None:
    deadline = time.monotonic() + milliseconds / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


def _wait_until(app,predicate,timeout_ms: int=1000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    app.processEvents()
    return bool(predicate())


def _optics_field(collection,section_key: str,name: str):
    return (
        collection.section_view(section_key)
        .groups["optics"]
        .binding.fields[(name,)]
    )


def test_runtime_view_binding_debounces_and_coalesces_real_field_edits():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=20,
    )
    applied = []
    binding.sigPatchApplied.connect(
        lambda section_key,update:
            applied.append((section_key,update))
    )

    try:
        field = _optics_field(collection,"sec_0","wavelength_nm")
        field.set_value(500,emit=True)
        field.set_value(510,emit=True)

        # Runtime stays committed until the single-shot debounce fires.
        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 488
        assert binding.pending_section_keys == ("sec_0",)

        assert _wait_until(
            app,
            lambda: runtime.get_section_snapshot(
                "sec_0"
            ).state.optics.wavelength_nm == 510,
        )

        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 510
        assert collection.get_section_snapshot(
            "sec_0"
        ).state.optics.wavelength_nm == 510
        assert field.value() == 510
        assert len(applied) == 1
        assert applied[0][0] == "sec_0"
        assert applied[0][1].normalized_values == {
            ("optics","wavelength_nm"):510
        }
        assert not binding.has_pending_patches
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_reconciles_feedback_before_applied_signal():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=100,
    )
    observed = []
    binding.sigPatchApplied.connect(
        lambda section_key,_update:observed.append(
            (
                section_key,
                collection.section_view(section_key)._feedback_status,
            )
        )
    )

    try:
        assert collection.section_view("sec_0")._feedback_status is None
        _optics_field(collection,"sec_0","wavelength_nm").set_value(
            500,emit=True,
        )
        binding.flush_section("sec_0",propagate=True)

        expected = runtime.get_section_feedback_status("sec_0")
        assert collection.section_view("sec_0")._feedback_status == expected
        assert observed == [("sec_0",expected)]
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_keeps_section_debounce_state_independent():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=20,
    )
    applied = []
    binding.sigPatchApplied.connect(
        lambda section_key,_update:applied.append(section_key)
    )

    try:
        _optics_field(collection,"sec_0","wavelength_nm").set_value(
            500,emit=True,
        )
        _optics_field(collection,"sec_1","wavelength_nm").set_value(
            520,emit=True,
        )
        assert set(binding.pending_section_keys) == {"sec_0","sec_1"}

        assert _wait_until(
            app,
            lambda: (
                runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 500
                and runtime.get_section_snapshot("sec_1").state.optics.wavelength_nm == 520
            ),
        )

        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 500
        assert runtime.get_section_snapshot("sec_1").state.optics.wavelength_nm == 520
        assert sorted(applied) == ["sec_0","sec_1"]
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_flush_is_an_explicit_ordering_barrier():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=100,
    )
    applied = []
    binding.sigPatchApplied.connect(
        lambda section_key,_update:applied.append(section_key)
    )

    try:
        _optics_field(collection,"sec_0","wavelength_nm").set_value(
            500,emit=True,
        )
        update = binding.flush_section("sec_0",propagate=True)

        assert update is not None
        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 500
        assert applied == ["sec_0"]

        # The stopped timer must not replay the already-flushed batch.
        assert not any(timer.isActive() for timer in binding._timers.values())
        app.processEvents()
        assert applied == ["sec_0"]
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_restores_authoritative_view_after_async_failure():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=10,
    )
    failures = []
    binding.sigPatchFailed.connect(
        lambda section_key,error:failures.append((section_key,error))
    )

    try:
        field = _optics_field(collection,"sec_0","wavelength_nm")
        field.set_value(700,emit=False)  # simulate an uncommitted editor draft
        collection.sigSectionPatchRequested.emit(
            "sec_0",{("unknown_group","value"):1},
        )

        assert _wait_until(app,lambda: len(failures) == 1)

        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 488
        assert _optics_field(
            collection, "sec_0", "wavelength_nm"
        ).value() == 488
        assert len(failures) == 1
        assert failures[0][0] == "sec_0"
        assert isinstance(failures[0][1],Exception)
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_propagating_failure_restores_without_async_signal():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=100,
    )
    failures = []
    binding.sigPatchFailed.connect(
        lambda section_key,error:failures.append((section_key,error))
    )

    try:
        field = _optics_field(collection,"sec_0","wavelength_nm")
        field.set_value(700,emit=False)
        collection.sigSectionPatchRequested.emit(
            "sec_0",{("unknown_group","value"):1},
        )

        with pytest.raises(Exception):
            binding.flush_section("sec_0",propagate=True)

        assert _optics_field(
            collection, "sec_0", "wavelength_nm"
        ).value() == 488
        assert failures == []
    finally:
        binding.dispose()
        collection.deleteLater()


def test_runtime_view_binding_cancel_and_dispose_drop_drafts_and_disconnect():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        debounce_ms=20,
    )
    applied = []
    binding.sigPatchApplied.connect(
        lambda section_key,_update:applied.append(section_key)
    )

    try:
        field = _optics_field(collection,"sec_0","wavelength_nm")
        field.set_value(500,emit=True)
        affected = binding.cancel_all(restore=True)

        restored_field = _optics_field(
            collection,"sec_0","wavelength_nm"
        )

        assert affected == ("sec_0",)
        assert restored_field.value() == 488
        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 488

        binding.dispose()
        restored_field.set_value(520,emit=True)
        app.processEvents()

        assert runtime.get_section_snapshot("sec_0").state.optics.wavelength_nm == 488
        assert applied == []
    finally:
        binding.dispose()
        collection.deleteLater()


def _cgh_group(collection,section_key="sec_0"):
    return collection.section_view(section_key).groups["cgh"]


def _select_target(collection,target="multi_foci",section_key="sec_0"):
    from slmcore.engine.section import CGHState

    field = _cgh_group(collection,section_key).binding.fields[
        CGHState.selected_target_path()
    ]
    field.set_value(target,emit=True)
    return field


def test_programmatic_target_selector_update_synchronizes_cgh_pages():
    _app()
    from slmcore.qt.sections.collection import SectionsCollectionView
    from slmcore.engine.section import CGHState

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    try:
        cgh = _cgh_group(collection)
        selector = cgh.binding.fields[CGHState.selected_target_path()]

        # Mimic retained-view reconciliation during config loading: the field is
        # updated silently rather than as a user edit.
        assert cgh.stack.currentIndex() == cgh._page_by_target[None]
        assert cgh.set_parameter(
            CGHState.selected_target_path(),"multi_foci_vector"
        )

        assert selector.value() == "multi_foci_vector"
        assert cgh.stack.currentIndex() == cgh._page_by_target[
            "multi_foci_vector"
        ]
        assert cgh._computation_stack.currentIndex() == (
            cgh._computation_page_by_target["multi_foci_vector"]
        )
    finally:
        collection.deleteLater()


def _target_field(
    collection,name: str,target="multi_foci",section_key="sec_0",
):
    return _cgh_group(collection,section_key).binding.fields[
        (target,"params",name)
    ]



def _runtime_target_value(
    runtime,name: str,target="multi_foci",section_key="sec_0",
):
    return (
        runtime.get_section_snapshot(section_key)
        .state.cgh.items[target].params.get_param_value(name)
    )


def test_target_selector_is_immediate_but_target_parameters_use_target_debounce():
    app = _app()
    from slmcore.qt import RuntimeViewInteractionSettings
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        interaction_settings=RuntimeViewInteractionSettings(
            standard_patch_debounce_ms=10,
            target_patch_debounce_ms=80,
        ),
    )

    try:
        _select_target(collection)
        assert runtime.get_section_snapshot(
            "sec_0"
        ).state.cgh.selected_target == "multi_foci"
        assert not binding.has_pending_patches

        field = _target_field(collection,"period_x_px")
        value = field.value()
        field.set_value(value + 1,emit=True)
        assert binding.pending_section_keys == ("sec_0",)
        assert _runtime_target_value(runtime,"period_x_px") == value

        from slmcore.qt.application.interaction import ParameterEditKind
        timer = binding._timer("sec_0",ParameterEditKind.CGH_TARGET)
        assert timer.isActive()
        assert timer.interval() == 80

        binding._flush_kind(
            "sec_0",ParameterEditKind.CGH_TARGET,propagate=True,
        )
        assert not binding.has_pending_patches
        assert _runtime_target_value(runtime,"period_x_px") != value
    finally:
        binding.dispose()
        collection.deleteLater()


def test_pending_target_edit_does_not_visibly_disable_other_editors():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=100,
    )

    try:
        selector = _select_target(collection)
        origin = _target_field(collection,"period_x_px")
        sibling = _target_field(collection,"n_foci_x")
        standard = _optics_field(collection,"sec_0","wavelength_nm")

        origin.set_value(origin.value() + 1,emit=True)

        assert origin.editor.isEnabled()
        assert sibling.editor.isEnabled()
        assert selector.editor.isEnabled()
        assert standard.editor.isEnabled()

        binding.flush_section("sec_0",propagate=True)
        assert selector.editor.isEnabled()
        assert standard.editor.isEnabled()
    finally:
        binding.dispose()
        collection.deleteLater()


def test_different_target_parameters_coalesce_into_one_target_transaction():
    app = _app()
    from slmcore.qt import RuntimeViewInteractionSettings
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        interaction_settings=RuntimeViewInteractionSettings(
            standard_patch_debounce_ms=10,
            target_patch_debounce_ms=30,
        ),
    )
    applied = []
    binding.sigPatchApplied.connect(
        lambda _section,update:applied.append(update)
    )

    try:
        _select_target(collection)
        applied.clear()

        period = _target_field(collection,"period_x_px")
        count = _target_field(collection,"n_foci_x")
        requested_period = period.value() + 1
        requested_count = count.value() + 10
        period.set_value(requested_period,emit=True)
        count.set_value(requested_count,emit=True)

        assert len(binding._pending_patches) == 1
        from slmcore.qt.application.interaction import ParameterEditKind
        binding._flush_kind(
            "sec_0",ParameterEditKind.CGH_TARGET,propagate=True,
        )

        assert len(applied) == 1
        assert applied[0].normalized_values[
            ("cgh","multi_foci","params","period_x_px")
        ] == requested_period
        assert applied[0].normalized_values[
            ("cgh","multi_foci","params","n_foci_x")
        ] == requested_count
        assert not binding.has_pending_patches
    finally:
        binding.dispose()
        collection.deleteLater()


def test_standard_and_target_debounce_buckets_are_independent():
    app = _app()
    from slmcore.qt import RuntimeViewInteractionSettings
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        interaction_settings=RuntimeViewInteractionSettings(
            standard_patch_debounce_ms=20,
            target_patch_debounce_ms=90,
        ),
    )

    try:
        _select_target(collection)
        target = _target_field(collection,"period_x_px")
        old_target = _runtime_target_value(runtime,"period_x_px")
        target.set_value(target.value() + 1,emit=True)
        _optics_field(collection,"sec_0","wavelength_nm").set_value(
            500,emit=True,
        )

        from slmcore.qt.application.interaction import ParameterEditKind
        standard_timer = binding._timer("sec_0",ParameterEditKind.STANDARD)
        target_timer = binding._timer("sec_0",ParameterEditKind.CGH_TARGET)
        assert standard_timer.isActive()
        assert target_timer.isActive()
        assert standard_timer.interval() == 20
        assert target_timer.interval() == 90

        binding._flush_kind(
            "sec_0",ParameterEditKind.STANDARD,propagate=True,
        )
        assert runtime.get_section_snapshot(
            "sec_0"
        ).state.optics.wavelength_nm == 500
        assert _runtime_target_value(runtime,"period_x_px") == old_target
        assert binding.has_pending_patches

        binding._flush_kind(
            "sec_0",ParameterEditKind.CGH_TARGET,propagate=True,
        )
        assert _runtime_target_value(runtime,"period_x_px") != old_target
        assert not binding.has_pending_patches
    finally:
        binding.dispose()
        collection.deleteLater()


def test_target_type_change_cancels_pending_target_batch_instead_of_flushing():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=100,
    )

    try:
        _select_target(collection,"multi_foci")
        field = _target_field(collection,"period_x_px")
        committed = _runtime_target_value(runtime,"period_x_px")
        field.set_value(committed + 1,emit=True)
        assert binding.has_pending_patches

        _select_target(collection,"multi_foci_vector")

        snapshot = runtime.get_section_snapshot("sec_0")
        assert snapshot.state.cgh.selected_target == "multi_foci_vector"
        assert _runtime_target_value(runtime,"period_x_px") == committed
        assert field.value() == committed
        assert not binding.has_pending_patches
    finally:
        binding.dispose()
        collection.deleteLater()


def test_target_spinbox_defers_typed_text_but_keeps_discrete_steps_live():
    _app()
    from qtpy import QtWidgets
    from slmcore.qt.application.interaction import ParameterCommitMode
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    try:
        field = _target_field(collection,"n_foci_x")
        assert field.commit_mode is ParameterCommitMode.EDIT_FINISHED
        assert isinstance(
            field.editor,(QtWidgets.QSpinBox,QtWidgets.QDoubleSpinBox),
        )

        changes = []
        field.sigValueChanged.connect(
            lambda _key,value:changes.append(value)
        )
        original = field.value()
        line_edit = field.editor.lineEdit()
        line_edit.setText(str(int(original) + 2))
        line_edit.textEdited.emit(line_edit.text())
        assert field.value() == original
        assert changes == []

        field.editor.editingFinished.emit()
        assert field.value() == int(original) + 2
        assert changes == [int(original) + 2]

        changes.clear()
        field.editor.stepUp()
        assert changes
    finally:
        collection.deleteLater()


def test_auto_compute_is_immediate_after_committed_target_edits():
    app = _app()
    from slmcore.qt import RuntimeViewInteractionSettings
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        interaction_settings=RuntimeViewInteractionSettings(
            standard_patch_debounce_ms=10,
            target_patch_debounce_ms=20,
        ),
    )
    requested = []
    binding.sigAutoComputeRequested.connect(requested.append)

    try:
        cgh = _cgh_group(collection)
        cgh.set_auto_recompute_enabled(True)

        _optics_field(collection,"sec_0","wavelength_nm").set_value(
            500,emit=True,
        )
        binding.flush_section("sec_0",propagate=True)
        assert requested == []

        _select_target(collection)
        # Target selection is immediate, so auto-compute is also immediate.
        assert requested == ["sec_0"]

        requested.clear()
        target = _target_field(collection,"period_x_px")
        target.set_value(target.value() + 1,emit=True)
        assert requested == []
        from slmcore.qt.application.interaction import ParameterEditKind
        binding._flush_kind(
            "sec_0",ParameterEditKind.CGH_TARGET,propagate=True,
        )
        assert requested == ["sec_0"]
    finally:
        binding.dispose()
        collection.deleteLater()


def test_explicit_flush_barrier_commits_target_without_auto_compute():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=100,
    )
    requested = []
    binding.sigAutoComputeRequested.connect(requested.append)

    try:
        cgh = _cgh_group(collection)
        cgh.set_auto_recompute_enabled(True)
        _select_target(collection)
        requested.clear()

        target = _target_field(collection,"period_x_px")
        old_value = _runtime_target_value(runtime,"period_x_px")
        target.set_value(target.value() + 1,emit=True)
        binding.flush_section("sec_0",propagate=True)

        assert _runtime_target_value(runtime,"period_x_px") != old_value
        assert requested == []
    finally:
        binding.dispose()
        collection.deleteLater()


def test_raster_lock_only_commit_does_not_request_auto_compute():
    _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=30,
    )
    requested = []
    binding.sigAutoComputeRequested.connect(requested.append)

    try:
        cgh = _cgh_group(collection)
        cgh.set_auto_recompute_enabled(True)
        _select_target(collection)
        requested.clear()

        target_state = runtime.get_section_snapshot(
            "sec_0"
        ).state.cgh.items["multi_foci"]
        expected_reference = (
            target_state.params.get_param_value("fov_x_px"),
            target_state.params.get_param_value("fov_y_px"),
        )
        fov_lock = cgh._lock_buttons["multi_foci"]["fov"]
        fov_lock.click()

        lock = runtime.get_section_snapshot(
            "sec_0"
        ).state.cgh.items["multi_foci"].lock_state
        assert lock.kind == "fov"
        assert lock.reference == expected_reference
        assert requested == []
    finally:
        binding.dispose()
        collection.deleteLater()


def test_lock_click_joins_pending_target_batch_and_captures_draft_row_values():
    app = _app()
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=30,
    )

    try:
        cgh = _cgh_group(collection)
        _select_target(collection)
        current_fov_y = _runtime_target_value(runtime,"fov_y_px")
        fov_x = _target_field(collection,"fov_x_px")
        fov_x.set_value(190.0,emit=True)
        cgh._lock_buttons["multi_foci"]["fov"].click()

        # Both remain draft state until the target debounce resolves.
        lock = runtime.get_section_snapshot(
            "sec_0"
        ).state.cgh.items["multi_foci"].lock_state
        assert lock.kind is None

        from slmcore.qt.application.interaction import ParameterEditKind
        binding._flush_kind(
            "sec_0",ParameterEditKind.CGH_TARGET,propagate=True,
        )
        lock = runtime.get_section_snapshot(
            "sec_0"
        ).state.cgh.items["multi_foci"].lock_state
        assert lock.kind == "fov"
        assert lock.reference == (190.0,current_fov_y)
    finally:
        binding.dispose()
        collection.deleteLater()


def test_only_raster_multi_foci_exposes_row_lock_controls():
    _app()
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    try:
        cgh = _cgh_group(collection)
        assert set(cgh._lock_buttons["multi_foci"]) == {"fov","n_foci"}
        assert "multi_foci_vector" not in cgh._lock_buttons

        fov = cgh._lock_buttons["multi_foci"]["fov"]
        n_foci = cgh._lock_buttons["multi_foci"]["n_foci"]
        fov.click()
        assert fov.isChecked()
        assert not n_foci.isChecked()
        n_foci.click()
        assert not fov.isChecked()
        assert n_foci.isChecked()
    finally:
        collection.deleteLater()


def test_actual_cgh_computation_disables_only_cgh_definition_controls():
    _app()
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    try:
        cgh = _cgh_group(collection)
        _select_target(collection)
        selector = cgh.binding.fields[("selected_target",)]
        target = _target_field(collection,"period_x_px")
        standard = _optics_field(collection,"sec_0","wavelength_nm")
        lock_button = cgh._lock_buttons["multi_foci"]["fov"]

        cgh.set_computing(True)
        assert not selector.editor.isEnabled()
        assert not target.editor.isEnabled()
        assert not lock_button.isEnabled()
        assert not cgh.compute_button.isEnabled()
        assert not cgh.auto_recompute_checkbox.isEnabled()
        assert standard.editor.isEnabled()

        cgh.set_computing(False)
        assert selector.editor.isEnabled()
        assert target.editor.isEnabled()
        assert lock_button.isEnabled()
        assert standard.editor.isEnabled()
    finally:
        collection.deleteLater()


def test_feedback_state_disables_and_suppresses_auto_compute(monkeypatch):
    _app()
    from dataclasses import replace
    from slmcore.qt import RuntimeViewInteractionSettings
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,
        section_collection=collection,
        interaction_settings=RuntimeViewInteractionSettings(
            standard_patch_debounce_ms=10,
            target_patch_debounce_ms=20,
        ),
    )
    requested = []
    binding.sigAutoComputeRequested.connect(requested.append)

    try:
        cgh = _cgh_group(collection)
        cgh.set_auto_recompute_enabled(True)
        blocked = replace(
            runtime.get_section_feedback_status("sec_0"),
            intensity_count=1,
        )
        monkeypatch.setattr(
            runtime,"get_section_feedback_status",lambda _key:blocked,
        )
        collection.set_feedback_status("sec_0",blocked)

        assert cgh.auto_recompute_enabled()
        assert not cgh.auto_recompute_checkbox.isEnabled()
        assert "feedback is active" in cgh.auto_recompute_checkbox.toolTip()

        _select_target(collection)
        assert requested == []
    finally:
        binding.dispose()
        collection.deleteLater()


def test_restore_current_target_cancels_pending_draft_and_restores_stale_target():
    _app()
    import numpy as np
    from slmcore.cgh import CGHResult,CGHResultState
    from slmcore.qt.application.runtime_binding import SLMRuntimeViewBinding
    from slmcore.qt.sections.collection import SectionsCollectionView

    runtime = _runtime(n_sections=1)
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    binding = SLMRuntimeViewBinding(
        runtime=runtime,section_collection=collection,debounce_ms=100,
    )

    try:
        _select_target(collection)
        job = runtime.prepare_section_cgh("sec_0")
        result = CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        )
        transition = runtime.commit_section_cgh("sec_0",result)
        assert transition is not None
        collection.apply_section_transition("sec_0",transition)

        target = _target_field(collection,"period_x_px")
        committed_value = _runtime_target_value(runtime,"period_x_px")
        target.set_value(committed_value + 1,emit=True)
        assert binding.has_pending_patches

        # Restore abandons the uncommitted draft rather than flushing it.
        assert binding.restore_current_cgh_target(
            "sec_0",propagate=True,
        ) is None
        assert not binding.has_pending_patches
        assert _target_field(
            collection,"period_x_px"
        ).value() == committed_value
        assert _runtime_target_value(runtime,"period_x_px") == committed_value

        # Once a target edit has committed and made the CGH stale, the same
        # action restores the committed target without launching a compute.
        target = _target_field(collection,"period_x_px")
        target.set_value(committed_value + 1,emit=True)
        binding.flush_section("sec_0",propagate=True)
        assert runtime.get_section_cgh_status(
            "sec_0"
        ).result_state is CGHResultState.STALE
        assert _cgh_group(collection).restore_target_button.isEnabled()

        update = binding.restore_current_cgh_target(
            "sec_0",propagate=True,
        )
        assert update is not None
        assert runtime.get_section_cgh_status(
            "sec_0"
        ).result_state is CGHResultState.CURRENT
        assert not _cgh_group(collection).restore_target_button.isEnabled()
    finally:
        binding.dispose()
        collection.deleteLater()

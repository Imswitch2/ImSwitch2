import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    FocusedEditingPreferences,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
)
from slmcore.core.engine.section import split_slm_geometry


def _app():
    pytest.importorskip("qtpy")
    try:
        from qtpy import QtWidgets
    except Exception as error:
        pytest.skip("Qt bindings are unavailable: %s" % error)
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def _runtime():
    geometry = SLMGeometry(width=12,height=10,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )


def test_focused_editing_is_visibility_only_and_restores_full_view():
    _app()
    from slmcore.qt.sections.view import SectionView

    runtime = _runtime()
    snapshot = runtime.get_section_snapshot("sec_0")
    view = SectionView(section_key="sec_0",snapshot=snapshot)
    requested = []
    view.sigPatchRequested.connect(lambda *args:requested.append(args))
    selection = FocusedEditingPreferences(
        groups=("cgh",),
        items={"patterns":("linear_phase","lens_phase")},
    )
    before = snapshot.state.to_dict()

    try:
        view.set_focused_editing(selection,enabled=True)

        assert requested == []
        assert view.snapshot.state.to_dict() == before
        assert view.groups["optics"].widget.isHidden()
        assert view.groups["corrections"].widget.isHidden()

        patterns = view.groups["patterns"]
        assert patterns.param_section.flat_mode
        assert patterns.widget.toggleButton.isHidden()
        assert patterns.widget.contentArea.isHidden()
        assert patterns.widget.contentArea.widget() is None
        assert patterns.widget.contentWidget.parentWidget() is patterns.widget
        assert not view.focused_editing_notice.isHidden()
        assert "rgba(255, 193, 7, 38)" in view.focused_editing_notice.styleSheet()
        assert "font-size: 10px" in view.focused_editing_notice.styleSheet()
        assert not patterns.widget.contentWidget.isEnabled()  # parent Patterns active=False
        assert patterns._item_widgets["linear_phase"].isFlat()
        assert "border: none" in patterns._item_widgets["linear_phase"].styleSheet()
        assert all(
            field.presentation_visible
            for field in patterns._item_forms["linear_phase"].fields.values()
            if field.definition.display_level.value != "advanced"
        )
        assert all(
            not field.presentation_visible
            for field in patterns._item_forms["binary_grating"].fields.values()
        )

        update = runtime.apply_section_patch(
            "sec_0",{("patterns","active"):True},
        )
        assert update is not None
        view.apply_update(update)
        assert patterns.widget.contentWidget.isEnabled()
        assert requested == []

        # A full authoritative restore must keep the presentation projection.
        view.restore_snapshot(runtime.get_section_snapshot("sec_0"))
        patterns = view.groups["patterns"]
        assert patterns.param_section.flat_mode
        assert view.groups["optics"].widget.isHidden()
        assert requested == []

        view.set_focused_editing(selection,enabled=False)
        assert not patterns.param_section.flat_mode
        assert not patterns.widget.toggleButton.isHidden()
        assert not patterns.widget.contentArea.isHidden()
        assert patterns.widget.contentArea.widget() is patterns.widget.contentWidget
        assert view.focused_editing_notice.isHidden()
        assert not patterns._item_widgets["linear_phase"].isFlat()
        assert patterns._item_widgets["linear_phase"].styleSheet() == ""
        assert not view.groups["optics"].widget.isHidden()
        assert all(
            field.presentation_visible
            for field in patterns._item_forms["binary_grating"].fields.values()
            if field.definition.display_level.value != "advanced"
        )
        assert requested == []
    finally:
        view.deleteLater()

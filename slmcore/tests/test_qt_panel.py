import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.engine.section import split_slm_geometry


def _app():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
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


def test_raw_preview_view_is_independently_usable():
    _app()
    from slmcore.qt import SLMPreviewView

    view = SLMPreviewView()
    frame = np.arange(120,dtype=np.uint8).reshape(10,12)
    try:
        view.set_frame(frame)
        np.testing.assert_array_equal(view.current_frame,frame)
        view.reset_view()
        view.clear_frame()
        assert view.current_frame is None
    finally:
        view.deleteLater()


def test_standard_slm_panel_composes_raw_preview_and_sections():
    _app()
    from slmcore.qt import SLMPanel

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
    )
    try:
        assert panel.preview_panel.view is panel.preview_view
        assert panel.section_host.collection is panel.section_collection
        assert panel.config_controls is not None
        assert not panel.connection_control_visible
    finally:
        panel.deleteLater()


def _runtime_two_sections():
    geometry = SLMGeometry(width=12,height=10,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2),
        registries=DEFAULT_REGISTRIES,
    )


def test_panel_layout_policy_rejects_side_collapsible_preview():
    _app()
    from slmcore.qt import PreviewContainer,PreviewPlacement,SLMPanelLayoutPolicy

    with pytest.raises(ValueError,match="only valid with TOP"):
        SLMPanelLayoutPolicy(
            preview_placement=PreviewPlacement.LEFT,
            preview_container=PreviewContainer.COLLAPSIBLE,
        )


def test_default_panel_preview_is_resizable_and_retains_exact_height():
    _app()
    from slmcore.qt import SLMPanel

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
    )
    try:
        section = panel.preview_panel.section
        assert section is not None
        assert section.resizable
        section.set_expanded(True,animate=False)
        assert section.content_height() == 220
        assert section.contentArea.minimumHeight() == 220
        assert section.contentArea.maximumHeight() == 220

        section.set_content_height(310)
        section.set_expanded(False,animate=False)
        section.set_expanded(True,animate=False)
        assert section.content_height() == 310
        assert section.contentArea.minimumHeight() == 310
        assert section.contentArea.maximumHeight() == 310
    finally:
        panel.deleteLater()


def test_plain_left_and_right_preview_use_horizontal_splitters():
    _app()
    from qtpy import QtCore
    from slmcore.qt import PreviewContainer,PreviewPlacement,SLMPanel,SLMPanelLayoutPolicy

    runtime = _runtime()
    for placement in (PreviewPlacement.LEFT,PreviewPlacement.RIGHT):
        panel = SLMPanel(
            section_snapshots=runtime.get_section_snapshots(),
            initial_frame=runtime.artifacts.eightbit,
            layout_policy=SLMPanelLayoutPolicy(
                preview_placement=placement,
                preview_container=PreviewContainer.PLAIN,
            ),
        )
        try:
            assert panel.body_splitter is not None
            assert panel.body_splitter.orientation() == QtCore.Qt.Horizontal
            preview_index = panel.body_splitter.indexOf(panel.preview_panel)
            assert preview_index == (0 if placement == PreviewPlacement.LEFT else 1)
        finally:
            panel.deleteLater()


def test_plain_top_preview_uses_vertical_splitter():
    _app()
    from qtpy import QtCore
    from slmcore.qt import PreviewContainer,PreviewPlacement,SLMPanel,SLMPanelLayoutPolicy

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
        layout_policy=SLMPanelLayoutPolicy(
            preview_placement=PreviewPlacement.TOP,
            preview_container=PreviewContainer.PLAIN,
        ),
    )
    try:
        assert panel.body_splitter is not None
        assert panel.body_splitter.orientation() == QtCore.Qt.Vertical
        assert panel.body_splitter.indexOf(panel.preview_panel) == 0
    finally:
        panel.deleteLater()


def test_preview_highlights_current_physical_section_only_in_tab_mode():
    _app()
    from slmcore.qt import SectionsDisplayMode,SLMPanel

    runtime = _runtime_two_sections()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
    )
    try:
        assert panel.preview_view.section_highlight_visible
        assert panel.preview_view.highlighted_section_key == "sec_0"

        panel.section_host.set_current_section_key("sec_1")
        assert panel.preview_view.section_highlight_visible
        assert panel.preview_view.highlighted_section_key == "sec_1"
        rect = panel.preview_view._section_highlight.rect()
        geometry = panel.section_host.section_geometries()["sec_1"]
        assert rect.x() == geometry.x
        assert rect.y() == geometry.y
        assert rect.width() == geometry.width
        assert rect.height() == geometry.height

        panel.section_host.set_display_mode(SectionsDisplayMode.HORIZONTAL)
        assert not panel.preview_view.section_highlight_visible
        assert panel.preview_view.highlighted_section_key is None

        panel.section_host.set_display_mode(SectionsDisplayMode.TABS)
        assert panel.preview_view.section_highlight_visible
        assert panel.preview_view.highlighted_section_key == "sec_1"
    finally:
        panel.deleteLater()



def test_fast_presentation_keeps_preview_state_and_hides_only_editor_sections():
    _app()
    from slmcore.qt import SLMPanel

    runtime = _runtime_two_sections()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
    )
    try:
        section = panel.preview_panel.section
        assert section is not None
        section.set_expanded(True,animate=False)
        section.set_content_height(287)
        assert panel.preview_view.section_highlight_visible

        panel.set_config_only_view(True)
        assert not panel.body_widget.isHidden()
        assert panel.sections_widget.isHidden()
        assert not panel.preview_panel.isHidden()
        assert section.expanded
        assert section.content_height() == 287
        assert not panel.preview_view.section_highlight_visible

        panel.set_config_only_view(False)
        assert not panel.sections_widget.isHidden()
        assert section.expanded
        assert section.content_height() == 287
        assert panel.preview_view.section_highlight_visible
    finally:
        panel.deleteLater()

def test_preview_none_constructs_no_preview_and_keeps_sections_available():
    _app()
    from slmcore.qt import PreviewPlacement,SLMPanel,SLMPanelLayoutPolicy

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
        layout_policy=SLMPanelLayoutPolicy(
            preview_placement=PreviewPlacement.NONE,
        ),
    )
    try:
        assert panel.preview_panel is None
        assert panel.preview_view is None
        panel.set_frame(np.ones(runtime.geometry.shape,dtype=np.uint8))
        assert panel.section_host.collection is panel.section_collection
    finally:
        panel.deleteLater()


def test_section_highlight_tracks_runtime_section_geometry_replacement():
    _app()
    from slmcore.qt import SLMPanel

    geometry = SLMGeometry(width=12,height=10,pixel_size_um=1.0)
    first = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2,axis="x"),
        registries=DEFAULT_REGISTRIES,
    )
    second = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2,axis="y"),
        registries=DEFAULT_REGISTRIES,
    )
    panel = SLMPanel(
        section_snapshots=first.get_section_snapshots(),
        initial_frame=first.artifacts.eightbit,
    )
    try:
        initial = panel.preview_view._section_highlight.rect()
        assert initial.width() == 6
        assert initial.height() == 10

        panel.replace_sections(second.get_section_snapshots())
        updated = panel.preview_view._section_highlight.rect()
        assert panel.preview_view.highlighted_section_key == "sec_0"
        assert updated.width() == 12
        assert updated.height() == 5
    finally:
        panel.deleteLater()

import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.engine.section import split_slm_geometry


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
    geometry = SLMGeometry(width=12,height=6,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,n_sections),
        registries=DEFAULT_REGISTRIES,
    )


def test_sections_view_host_switches_modes_without_recreating_views():
    app =_app()
    from slmcore.qt import SectionsDisplayMode
    from slmcore.qt.sections.collection import SectionsCollectionView
    from slmcore.qt.sections.view_host import SectionsViewHost

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    host = SectionsViewHost(collection,show_settings=True)
    try:
        host.resize(900,500)
        host.show()
        view = collection.section_view("sec_0")

        assert host.display_mode == SectionsDisplayMode.TABS
        assert view.parent() is not None

        host.set_display_mode(SectionsDisplayMode.HORIZONTAL)
        app.processEvents()

        assert host.display_mode == SectionsDisplayMode.HORIZONTAL
        assert collection.section_view("sec_0") is view
        assert view.parent() is not None
        assert not view.isHidden()
        assert view.isVisible()

        host.set_display_mode(SectionsDisplayMode.TABS)
        app.processEvents()

        assert collection.section_view("sec_0") is view
        assert view.parent() is not None
    finally:
        host.deleteLater()


def test_sections_view_host_replacement_detaches_previous_views():
    _app()
    from slmcore.qt.sections.collection import SectionsCollectionView
    from slmcore.qt.sections.view_host import SectionsViewHost

    old_runtime = _runtime()
    old_collection = SectionsCollectionView(
        section_snapshots=old_runtime.get_section_snapshots(),
    )
    host = SectionsViewHost(old_collection,show_settings=False)
    old_view = old_collection.section_view("sec_0")

    new_runtime = _runtime()
    new_collection = SectionsCollectionView(
        section_snapshots=new_runtime.get_section_snapshots(),
    )

    try:
        host.set_section_title("sec_0","Left")
        host.set_collection(new_collection)

        assert old_view.parent() is None
        assert new_collection.section_view("sec_0").parent() is not None
        assert host.section_title("sec_0") == "Left"
    finally:
        old_view.deleteLater()
        old_collection.deleteLater()
        host.deleteLater()


def test_slm_sections_settings_dialog_reports_display_mode_change():
    _app()
    from slmcore.qt import SectionsDisplayMode
    from slmcore.qt.sections.settings import SLMSectionsSettingsDialog

    runtime = _runtime()
    dialog = SLMSectionsSettingsDialog(
        section_snapshots=runtime.get_section_snapshots(),
        display_mode=SectionsDisplayMode.TABS,
    )
    try:
        combo = dialog._display_mode_combo
        assert combo is not None
        combo.setCurrentIndex(
            combo.findData(SectionsDisplayMode.HORIZONTAL.value)
        )

        changes = dialog.changes()

        assert changes
        assert changes.display_mode == SectionsDisplayMode.HORIZONTAL
        assert changes.layout is None
    finally:
        dialog.deleteLater()


def test_sections_view_host_exposes_geometry_and_change_signals():
    _app()
    from slmcore.qt import SectionsDisplayMode
    from slmcore.qt.sections.collection import SectionsCollectionView
    from slmcore.qt.sections.view_host import SectionsViewHost

    runtime = _runtime()
    collection = SectionsCollectionView(
        section_snapshots=runtime.get_section_snapshots(),
    )
    host = SectionsViewHost(collection,show_settings=False)
    mode_changes = []
    section_changes = []
    host.sigDisplayModeChanged.connect(mode_changes.append)
    host.sigSectionsChanged.connect(lambda:section_changes.append(True))

    replacement_runtime = _runtime()
    replacement = SectionsCollectionView(
        section_snapshots=replacement_runtime.get_section_snapshots(),
    )
    try:
        geometries = host.section_geometries()
        assert tuple(geometries) == collection.section_keys
        assert geometries["sec_0"] == runtime.get_section_snapshot("sec_0").geometry

        host.set_display_mode(SectionsDisplayMode.HORIZONTAL)
        assert mode_changes == [SectionsDisplayMode.HORIZONTAL]

        host.set_collection(replacement)
        assert section_changes == [True]
        assert host.section_geometries()["sec_1"] == (
            replacement_runtime.get_section_snapshot("sec_1").geometry
        )
    finally:
        host.deleteLater()
        collection.deleteLater()

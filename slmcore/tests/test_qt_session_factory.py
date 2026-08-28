import json

import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMStartupPreferences,
    SLMWorkspace,
)
from slmcore.application import SLMRuntimeFactory
from slmcore.host import SLMDeviceProvider,SLMHostServices
from slmcore.core.engine.section import SectionSplitLayout


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


def _setup():
    return SLMSetup(
        identity=SLMIdentity("slm","SER123","Test SLM"),
        geometry=SLMGeometry(width=24,height=12,pixel_size_um=1.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
    )


def _runtime(setup,correction_provider=None):
    return SLMRuntimeFactory(
        setup=setup,
        registries=DEFAULT_REGISTRIES,
        correction_provider=correction_provider,
    ).create_default()


def _discard(_preferences):
    pass


def _write_setup_file(path,setup,preferences=None):
    preferences = preferences or SLMStartupPreferences()
    path.write_text(json.dumps({
        "schema_version":1,
        "setup":setup.to_dict(),
        "startup_preferences":preferences.to_dict(),
    }),encoding="utf-8")


def test_session_factory_constructs_default_session_and_panel():
    _app()
    from slmcore.qt import SLMPanel,SLMQtSession,SLMQtSessionFactory

    setup = _setup()
    factory = SLMQtSessionFactory()
    session,panel = factory.create(
        setup=setup,
        on_startup_preferences_changed=_discard,
    )
    try:
        assert isinstance(session,SLMQtSession)
        assert isinstance(panel,SLMPanel)
        assert session.panel is panel
        assert session.runtime.identity == setup.identity
        assert not session.configuration_available
        assert session.current_config_path is None
        assert session.display_name == "Test SLM"
    finally:
        session.dispose()
        panel.deleteLater()


def test_session_factory_uses_startup_preferences(tmp_path):
    _app()
    from slmcore.qt import SectionsDisplayMode,SLMQtSessionFactory

    setup = _setup()
    workspace = SLMWorkspace(tmp_path)
    store = workspace.config_store(setup.identity,DEFAULT_REGISTRIES)
    runtime = _runtime(setup,workspace.correction_store(setup.identity))
    store.save("startup.h5",runtime.create_config(),"startup",overwrite=False)
    preferences = SLMStartupPreferences(
        startup_config="startup.h5",
        section_display_mode="horizontal",
    )

    factory = SLMQtSessionFactory(workspace=workspace)
    session,panel = factory.create(
        setup=setup,
        startup_preferences=preferences,
        on_startup_preferences_changed=_discard,
    )
    try:
        assert session.current_config_path == str(store.resolve("startup.h5"))
        assert session.configuration_available
        assert session.config_directory == tmp_path / "configs" / "SER123"
        assert session.resolve_config_path("startup.h5") == str(store.resolve("startup.h5"))
        assert panel.section_host.display_mode == SectionsDisplayMode.HORIZONTAL
    finally:
        session.dispose()
        panel.deleteLater()


def test_factory_automatically_persists_preferences_to_setup_file(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory

    setup = _setup()
    path = tmp_path / "slm.json"
    _write_setup_file(path,setup)
    workspace = SLMWorkspace(tmp_path / "workspace")
    session,panel = SLMQtSessionFactory(workspace=workspace).create(
        setup=setup,
        startup_preferences=SLMStartupPreferences(),
        setup_file=path,
    )
    try:
        session.startup_preferences.set_section_display_mode("horizontal")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["startup_preferences"]["section_display_mode"] == "horizontal"
    finally:
        session.dispose()
        panel.deleteLater()


def test_factory_requires_persistence_path_or_host_callback():
    _app()
    from slmcore.qt import SLMQtSessionFactory

    with pytest.raises(ValueError,match="setup_file"):
        SLMQtSessionFactory().create(setup=_setup())


def test_session_factory_surfaces_startup_warning_without_workspace():
    _app()
    from slmcore.qt import SLMQtSessionFactory

    setup = _setup()
    session,panel = SLMQtSessionFactory().create(
        setup=setup,
        startup_preferences=SLMStartupPreferences(startup_config="missing.h5"),
        on_startup_preferences_changed=_discard,
    )
    try:
        assert "configuration storage is not configured" in (
            panel.status_label.full_text().lower()
        )
    finally:
        session.dispose()
        panel.deleteLater()


def test_session_factory_does_not_initialize_device():
    _app()
    from slmcore.qt import SLMQtSessionFactory

    uploads = []
    device = SLMDeviceProvider(
        upload_frame=lambda frame:uploads.append(frame.copy()),
    )
    session,panel = SLMQtSessionFactory().create(
        setup=_setup(),
        host_services=SLMHostServices(device=device),
        on_startup_preferences_changed=_discard,
    )
    try:
        assert uploads == []
        session.initialize_device(show_error=False)
        assert len(uploads) == 1
    finally:
        session.dispose()
        panel.deleteLater()


def test_session_factory_cleans_panel_when_session_construction_fails(monkeypatch):
    _app()
    import slmcore.qt.application.factory as factory_module

    deleted = []

    class FakePanel:
        def __init__(self,**_kwargs):
            pass

        def deleteLater(self):
            deleted.append(True)

    def fail_session(**_kwargs):
        raise RuntimeError("session construction failed")

    monkeypatch.setattr(factory_module,"SLMPanel",FakePanel)
    monkeypatch.setattr(factory_module,"SLMQtSession",fail_session)

    factory = factory_module.SLMQtSessionFactory()
    with pytest.raises(RuntimeError,match="session construction failed"):
        factory.create(
            setup=_setup(),
            on_startup_preferences_changed=_discard,
        )
    assert deleted == [True]


def test_session_factory_accepts_custom_registries():
    _app()
    from slmcore.qt import SLMQtSessionFactory
    from slmcore.core.engine.registry import SLMRegistries

    registries = SLMRegistries()
    factory = SLMQtSessionFactory(registries=registries)
    session,panel = factory.create(
        setup=_setup(),
        on_startup_preferences_changed=_discard,
    )
    try:
        assert session.runtime.registries is registries
    finally:
        session.dispose()
        panel.deleteLater()


def test_slmcore_qt_top_level_is_strict_host_facing_api():
    _app()
    import slmcore.qt as qt

    assert set(qt.__all__) == {
        "DEFAULT_RENDER_POLICY",
        "DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS",
        "DEFAULT_SLM_PANEL_LAYOUT_POLICY",
        "PreviewContainer",
        "PreviewPlacement",
        "RenderPolicy",
        "RuntimeViewInteractionSettings",
        "SectionsDisplayMode",
        "SLMPanel",
        "SLMPanelLayoutPolicy",
        "SLMPreviewPanel",
        "SLMPreviewView",
        "SLMQtSession",
        "SLMQtSessionFactory",
        "SLMQtSessionGroup",
        "SLMControlMode",
        "SLMControlModeSelector",
    }
    assert not hasattr(qt,"ParamForm")
    assert not hasattr(qt,"SectionsCollectionView")
    assert not hasattr(qt,"SLMRuntimeViewBinding")

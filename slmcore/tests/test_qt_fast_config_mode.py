import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,SLM_CONFIG_SCHEMA_VERSION,SLMConfig,SLMGeometry,SLMIdentity,
    SLMSectionsSetup,SLMSetup,SLMWorkspace,
)
from slmcore.core.engine.section import SectionSplitLayout
from slmcore.host import MockSLMDeviceProvider,SLMHostServices


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


def _setup(key="slm",serial="SER123"):
    return SLMSetup(
        identity=SLMIdentity(key,serial),
        geometry=SLMGeometry(width=24,height=12,pixel_size_um=1.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
    )


def _create_session(tmp_path,key="slm",serial="SER123"):
    _app()
    from slmcore.qt import SLMQtSessionFactory

    device = MockSLMDeviceProvider()
    setup = _setup(key,serial)
    workspace = SLMWorkspace(tmp_path)
    store = workspace.config_store(setup.identity,DEFAULT_REGISTRIES)
    session,panel = SLMQtSessionFactory(
        workspace=workspace,
    ).create(
        setup=setup,
        host_services=SLMHostServices(device=device),
        on_startup_preferences_changed=lambda _preferences:None,
    )
    return session,panel,device,store


def _save_compiled(session,store,name,value,*,pixel_size_um=None,identity=None):
    base = session.runtime.create_config()
    geometry = base.geometry
    if pixel_size_um is not None:
        geometry = SLMGeometry(
            width=geometry.width,
            height=geometry.height,
            pixel_size_um=float(pixel_size_um),
        )
    config = SLMConfig(
        schema_version=SLM_CONFIG_SCHEMA_VERSION,
        identity=base.identity if identity is None else identity,
        geometry=geometry,
        sections=base.sections,
        final_eightbit=np.full(base.geometry.shape,value,dtype=np.uint8),
    )
    return store.save(name,config,"compiled",overwrite=False).path


def test_fast_switch_uploads_saved_frame_without_mutating_editor_runtime(tmp_path):
    _app()
    from slmcore.qt import SLMControlMode

    session,panel,device,store = _create_session(tmp_path)
    frames = []
    session.sigFrameChanged.connect(lambda frame:frames.append(np.array(frame,copy=True)))
    try:
        path_a = _save_compiled(session,store,"a.h5",17)
        path_b = _save_compiled(session,store,"b.h5",93)
        assert session.load_config(
            str(path_a),confirm_layout_change=False,
            calibration_mismatch_policy="reject",
        )

        assert session.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert session.current_config_path == str(path_a)
        assert session.fast_config_path == str(path_a)
        np.testing.assert_array_equal(
            device.last_frame,np.full(session.runtime.geometry.shape,17,dtype=np.uint8),
        )
        assert not panel.body_widget.isHidden()
        assert panel.sections_widget.isHidden()
        assert not panel.preview_panel.isHidden()
        assert not panel.preview_view.section_highlight_visible
        assert not panel.config_controls.update_button.isEnabled()
        assert not panel.config_controls.save_as_action.isEnabled()
        assert not panel.config_controls.rename_action.isEnabled()
        assert not panel.config_controls.duplicate_action.isEnabled()
        assert not panel.config_controls.delete_action.isEnabled()
        assert not panel.config_controls.startup_action.isEnabled()

        revision = session.runtime.revision
        snapshots = session.runtime.get_section_snapshots()
        upload_count = device.upload_count
        frame_count = len(frames)

        assert session.activate_compiled_config(str(path_b))
        assert session.fast_config_path == str(path_b)
        assert session.current_config_path == str(path_a)
        assert session.runtime.revision == revision
        assert session.runtime.get_section_snapshots() == snapshots
        assert device.upload_count == upload_count + 1
        np.testing.assert_array_equal(
            device.last_frame,np.full(session.runtime.geometry.shape,93,dtype=np.uint8),
        )
        np.testing.assert_array_equal(frames[-1],device.last_frame)

        # Runtime publication is a hard no-op while fast mode owns output.
        assert not session.upload_current_frame()
        session.publish_current_frame()
        assert device.upload_count == upload_count + 1
        assert len(frames) == frame_count + 1
        assert not session._binding.writes_enabled

        # Public full-load API does not silently change meaning in fast mode.
        session.sigWarning.disconnect(panel.show_warning)
        assert not session.load_config(str(path_a),show_error=False)
        assert session.current_config_path == str(path_a)

        assert session.set_control_mode(SLMControlMode.EDITOR)
        assert session.fast_config_path is None
        assert session.current_config_path == str(path_b)
        assert not panel.body_widget.isHidden()
        assert not panel.sections_widget.isHidden()
        assert session._binding.writes_enabled
    finally:
        session.dispose()
        panel.deleteLater()


def test_fast_activation_rejects_wrong_identity_without_changing_selection(tmp_path):
    _app()
    from slmcore.qt import SLMControlMode

    session,panel,_device,store = _create_session(tmp_path)
    errors = []
    session.sigError.disconnect(panel.show_error)
    session.sigError.connect(lambda title,error:errors.append((title,error)))
    try:
        path_a = _save_compiled(session,store,"a.h5",17)
        wrong = _save_compiled(
            session,store,"wrong.h5",44,
            identity=SLMIdentity("other","SER123"),
        )
        assert session.load_config(
            str(path_a),confirm_layout_change=False,
            calibration_mismatch_policy="reject",
        )
        assert session.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert not session.activate_compiled_config(str(wrong))
        assert session.fast_config_path == str(path_a)
        assert errors
    finally:
        session.dispose()
        panel.deleteLater()
        
def test_fast_activation_rejects_wrong_geometry_without_changing_selection(tmp_path):
    _app()
    from slmcore.qt import SLMControlMode

    session,panel,_device,store = _create_session(tmp_path)
    errors = []
    session.sigError.disconnect(panel.show_error)
    session.sigError.connect(lambda title,error:errors.append((title,error)))

    try:
        path_a = _save_compiled(session,store,"a.h5",17)
        wrong = _save_compiled(
            session,
            store,
            "wrong_geometry.h5",
            44,
            pixel_size_um=7.5,
        )

        assert session.load_config(
            str(path_a),
            confirm_layout_change=False,
            calibration_mismatch_policy="reject",
        )

        assert session.set_control_mode(SLMControlMode.FAST_CONFIG)

        assert not session.activate_compiled_config(str(wrong))
        assert session.fast_config_path == str(path_a)
        assert errors

    finally:
        session.dispose()
        panel.deleteLater()

def test_session_group_is_optional_and_rolls_back_failed_mode_change(tmp_path,monkeypatch):
    _app()
    from slmcore.qt import SLMControlMode,SLMQtSessionGroup

    session_a,panel_a,_device_a,store_a = _create_session(tmp_path / "a","a","A")
    session_b,panel_b,_device_b,store_b = _create_session(tmp_path / "b","b","B")
    group = SLMQtSessionGroup()
    try:
        path_a = _save_compiled(session_a,store_a,"a.h5",11)
        path_b = _save_compiled(session_b,store_b,"b.h5",22)
        assert session_a.load_config(str(path_a),confirm_layout_change=False)
        assert session_b.load_config(str(path_b),confirm_layout_change=False)
        group.add_session(session_a,key="a")
        group.add_session(session_b,key="b")

        original = session_b.set_control_mode
        monkeypatch.setattr(session_b,"set_control_mode",lambda _mode:False)
        assert not group.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert group.control_mode is SLMControlMode.EDITOR
        assert session_a.control_mode is SLMControlMode.EDITOR
        assert session_b.control_mode is SLMControlMode.EDITOR

        monkeypatch.setattr(session_b,"set_control_mode",original)
        assert group.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert session_a.control_mode is SLMControlMode.FAST_CONFIG
        assert session_b.control_mode is SLMControlMode.FAST_CONFIG
    finally:
        group.remove_session("a")
        group.remove_session("b")
        session_a.dispose()
        session_b.dispose()
        panel_a.deleteLater()
        panel_b.deleteLater()


def test_configless_fast_entry_clears_preview_until_compiled_config_is_selected(tmp_path):
    _app()
    from slmcore.qt import SLMControlMode

    session,panel,_device,store = _create_session(tmp_path)
    try:
        assert panel.preview_view.current_frame is not None
        assert session.current_config_path is None

        assert session.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert session.fast_config_path is None
        assert panel.preview_view.current_frame is None
        assert not panel.body_widget.isHidden()
        assert panel.sections_widget.isHidden()

        path = _save_compiled(session,store,"selected.h5",71)
        assert session.activate_compiled_config(str(path))
        np.testing.assert_array_equal(
            panel.preview_view.current_frame,
            np.full(session.runtime.geometry.shape,71,dtype=np.uint8),
        )
    finally:
        session.dispose()
        panel.deleteLater()

import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.cgh import CGHResult
from slmcore.host import SLMDeviceProvider,SLMHostServices
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


def _runtime() -> SLMRuntime:
    geometry = SLMGeometry(width=32,height=32,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch(
        "sec_0",
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci",
        },
    )
    return runtime


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Executor:
    def __init__(self):
        self.submissions = []
        self.disposed = False

    def submit(self,job,on_result,on_error):
        handle = _Handle()
        self.submissions.append((job,on_result,on_error,handle))
        return handle

    def complete(self,index=-1):
        job,on_result,_on_error,_handle = self.submissions[index]
        pattern = np.ones(job.spec.context.shape,dtype=np.complex128)
        on_result(CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=pattern,
        ))

    def dispose(self):
        self.disposed = True


def _session(*,auto_upload_frame=True,executor=None):
    _app()
    from slmcore.qt import SLMPanel,SLMQtSession

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=runtime.artifacts.eightbit,
    )
    uploads = []
    session = SLMQtSession(
        runtime=runtime,
        panel=panel,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(
                upload_frame=lambda frame: uploads.append(
                    np.array(frame,copy=True)
                ),
            ),
        ),
        cgh_executor=executor,
        auto_upload_frame=auto_upload_frame,
    )
    return runtime,panel.section_collection,session,uploads


def test_cgh_action_dispatch_and_lifecycle_stay_inside_qt_session():
    app = _app()
    executor = _Executor()
    runtime,collection,session,uploads = _session(executor=executor)
    computing = []
    session.sigCghComputingChanged.connect(
        lambda section_key,value:computing.append((section_key,value))
    )

    try:
        collection.sigCghActionRequested.emit("sec_0","compute",{})
        app.processEvents()

        assert len(executor.submissions) == 1
        assert session.is_cgh_computing("sec_0")
        assert computing[-1] == ("sec_0",True)

        executor.complete()
        app.processEvents()

        result = runtime.get_section_cgh_result_copy("sec_0")
        assert result is not None
        assert not session.is_cgh_computing("sec_0")
        assert computing[-1] == ("sec_0",False)
        assert len(uploads) == 1
    finally:
        session.dispose()
        collection.deleteLater()


def test_cancelled_cgh_result_is_inert_even_if_executor_calls_back_late():
    app = _app()
    executor = _Executor()
    runtime,collection,session,_uploads = _session(executor=executor)

    try:
        session.compute_base_cgh("sec_0",confirm_feedback_loss=False)
        assert len(executor.submissions) == 1
        handle = executor.submissions[0][3]

        assert session.cancel_cgh("sec_0")
        assert handle.cancelled
        assert not session.is_cgh_computing("sec_0")

        executor.complete(0)
        app.processEvents()
        assert runtime.get_section_cgh_result_copy("sec_0") is None
    finally:
        session.dispose()
        collection.deleteLater()


def test_injected_executor_is_not_owned_by_qt_session():
    executor = _Executor()
    _runtime_value,collection,session,_uploads = _session(
        executor=executor,
    )
    session.dispose()
    collection.deleteLater()

    assert not executor.disposed


def test_auto_upload_policy_keeps_preview_publication_independent():
    _app()
    runtime,collection,session,uploads = _session(
        auto_upload_frame=False,
        executor=_Executor(),
    )
    frames = []
    session.sigFrameChanged.connect(
        lambda frame:frames.append(np.array(frame,copy=True))
    )

    try:
        session.publish_current_frame()
        assert len(frames) == 1
        assert uploads == []

        session.upload_current_frame()
        assert len(uploads) == 1
        np.testing.assert_array_equal(uploads[0],runtime.artifacts.eightbit)
    finally:
        session.dispose()
        collection.deleteLater()


def test_defer_frame_upload_coalesces_physical_upload_only():
    _app()
    _runtime_value,collection,session,uploads = _session(
        auto_upload_frame=True,
        executor=_Executor(),
    )
    frames = []
    session.sigFrameChanged.connect(lambda frame:frames.append(frame))

    try:
        with session.defer_frame_upload():
            session.publish_current_frame()
            session.publish_current_frame()

        assert len(frames) == 2
        assert len(uploads) == 1
    finally:
        session.dispose()
        collection.deleteLater()


def test_panel_session_owns_standard_view_and_device_wiring():
    app = _app()
    from slmcore.qt import SLMPanel,SLMQtSession

    runtime = _runtime()
    panel = SLMPanel(
        section_snapshots=runtime.get_section_snapshots(),
        initial_frame=np.zeros_like(runtime.artifacts.eightbit),
    )
    events = []
    uploads = []
    device = SLMDeviceProvider(
        upload_frame=lambda frame:uploads.append(np.array(frame,copy=True)),
        connect=lambda:(events.append("connect") or (True,"SER123")),
        disconnect=lambda:(events.append("disconnect") or (True,"closed")),
        requires_explicit_connection=True,
    )
    session = SLMQtSession(
        runtime=runtime,
        panel=panel,
        host_services=SLMHostServices(device=device),
        cgh_executor=_Executor(),
    )

    try:
        assert panel.connection_control_visible
        panel._connection_button.setChecked(True)
        app.processEvents()
        assert events == ["connect"]
        assert panel.connection_state
        assert len(uploads) == 1

        session.publish_current_frame()
        app.processEvents()
        np.testing.assert_array_equal(
            panel.preview_view.current_frame,runtime.artifacts.eightbit,
        )

        panel._connection_button.setChecked(False)
        app.processEvents()
        assert events == ["connect","disconnect"]
        assert not panel.connection_state
    finally:
        session.dispose()
        panel.deleteLater()

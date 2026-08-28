import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
    SLMSession,
    SLMSessionCallbacks,
)
from slmcore.cgh import CGHResult
from slmcore.engine.section import split_slm_geometry
from slmcore.host import SLMDeviceProvider,SLMHostServices


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
        pattern = np.full(job.spec.context.shape,1j,dtype=np.complex128)
        on_result(CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=pattern,
        ))

    def fail(self,error,index=-1):
        _job,_on_result,on_error,_handle = self.submissions[index]
        on_error(error)

    def dispose(self):
        self.disposed = True


def test_headless_session_owns_cgh_commit_and_frame_publication():
    runtime = _runtime()
    executor = _Executor()
    uploads = []
    frames = []
    transitions = []
    computing = []
    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(
                upload_frame=lambda frame:uploads.append(np.array(frame,copy=True)),
            ),
        ),
        cgh_executor=executor,
        callbacks=SLMSessionCallbacks(
            on_frame_changed=lambda frame:frames.append(np.array(frame,copy=True)),
            on_transition_committed=lambda key,transition:transitions.append((key,transition)),
            on_cgh_computing_changed=lambda key,value:computing.append((key,value)),
        ),
    )

    assert session.compute_base_cgh("sec_0")
    assert session.is_cgh_computing("sec_0")
    assert computing[-1] == ("sec_0",True)

    executor.complete()

    assert runtime.get_section_cgh_result_copy("sec_0") is not None
    assert not session.is_cgh_computing("sec_0")
    assert computing[-1] == ("sec_0",False)
    assert transitions and transitions[-1][0] == "sec_0"
    assert len(frames) == 1
    assert len(uploads) == 1


def test_cancelled_headless_cgh_result_is_inert():
    runtime = _runtime()
    executor = _Executor()
    session = SLMSession(runtime=runtime,cgh_executor=executor)

    assert session.compute_base_cgh("sec_0")
    handle = executor.submissions[0][3]
    assert session.cancel_cgh("sec_0")
    assert handle.cancelled

    executor.complete(0)
    assert runtime.get_section_cgh_result_copy("sec_0") is None


def test_presentation_callback_failure_does_not_invalidate_committed_cgh():
    runtime = _runtime()
    executor = _Executor()
    uploads = []
    errors = []
    finished = []

    def broken_presenter(_section_key,_transition):
        raise RuntimeError("presentation failed")

    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(upload_frame=lambda frame:uploads.append(frame)),
        ),
        cgh_executor=executor,
        callbacks=SLMSessionCallbacks(
            on_transition_committed=broken_presenter,
            on_error=lambda title,error:errors.append((title,error)),
        ),
    )

    assert session.compute_base_cgh(
        "sec_0",on_finished=lambda success,error:finished.append((success,error)),
    )
    executor.complete()

    assert runtime.get_section_cgh_result_copy("sec_0") is not None
    assert len(uploads) == 1
    assert finished == [(True,None)]
    assert errors
    assert "presentation" in errors[-1][0].lower()


def test_headless_deferred_upload_coalesces_hardware_only():
    runtime = _runtime()
    uploads = []
    frames = []
    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(upload_frame=lambda frame:uploads.append(frame)),
        ),
        callbacks=SLMSessionCallbacks(on_frame_changed=lambda frame:frames.append(frame)),
    )

    with session.defer_frame_upload():
        session.publish_current_frame()
        session.publish_current_frame()

    assert len(frames) == 2
    assert len(uploads) == 1


def test_headless_upload_failure_is_recorded_and_reported():
    runtime = _runtime()
    upload_errors = []
    errors = []

    def fail_upload(_frame):
        raise RuntimeError("device failed")

    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(upload_frame=fail_upload),
        ),
        callbacks=SLMSessionCallbacks(
            on_upload_failed=upload_errors.append,
            on_error=lambda title,error:errors.append((title,error)),
        ),
    )

    assert not session.upload_current_frame()
    assert isinstance(session.last_upload_error,RuntimeError)
    assert upload_errors == [session.last_upload_error]
    assert errors[-1][0] == "SLM frame upload failed"


def test_headless_session_owns_explicit_device_lifecycle():
    runtime = _runtime()
    events = []
    uploads = []
    device = SLMDeviceProvider(
        upload_frame=lambda frame:uploads.append(frame),
        connect=lambda:(events.append("connect") or (True,"SER123")),
        disconnect=lambda:(events.append("disconnect") or (True,"closed")),
        requires_explicit_connection=True,
    )
    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(device=device),
    )

    connected = session.connect_device()
    assert connected.connected
    assert events == ["connect"]
    assert len(uploads) == 1

    disconnected = session.disconnect_device()
    assert not disconnected.connected
    assert events == ["connect","disconnect"]

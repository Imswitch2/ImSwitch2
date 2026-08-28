import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,ImageMeasurement,SLMGeometry,SLMIdentity,SLMRuntime,
    SLMSession,SLMSessionCallbacks,
)
from slmcore.cgh import CGHResult
from slmcore.engine.section import split_slm_geometry
from slmcore.host import SLMDeviceProvider,SLMHostServices


def _runtime():
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
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
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","n_foci_x"):2,
            ("cgh","multi_foci_vector","params","n_foci_y"):2,
        },
    )
    job = runtime.prepare_section_base_cgh("sec_0")
    pattern = np.ones(job.spec.context.shape,dtype=np.complex128)
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=pattern,
        ),
    )
    return runtime


class _Request:
    def __init__(self):
        self._active = True
        self.cancelled = False

    @property
    def active(self):
        return self._active

    def cancel(self):
        self.cancelled = True
        self._active = False


class _Dispatcher:
    available = True

    def __init__(self):
        self.pending = []

    def available_sources(self,section_key):
        return ("cam",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        request = _Request()
        self.pending.append((request,on_result,on_error,metadata))
        return request

    def complete(self,measurement,index=-1):
        request,on_result,_on_error,_metadata = self.pending[index]
        request._active = False
        on_result(measurement)


def test_feedback_service_commits_measurement_without_qt():
    runtime = _runtime()
    changed = []
    session = SLMSession(
        runtime=runtime,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=changed.append,
        ),
    )
    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="test",
    )

    previous = session.feedback.commit_measurement("sec_0",measurement)

    assert previous is False
    inspection = runtime.get_section_feedback_inspection("sec_0")
    assert inspection.measurement is not None
    assert inspection.measurement.acquisition.measurement_id == measurement.measurement_id
    assert changed[-1] == "sec_0"


def test_feedback_measurement_dispatch_is_host_neutral_and_cancellable():
    runtime = _runtime()
    dispatcher = _Dispatcher()
    busy = []
    session = SLMSession(
        runtime=runtime,
        measurement_dispatcher=dispatcher,
        callbacks=SLMSessionCallbacks(
            on_feedback_measurement_busy_changed=(
                lambda key,value,message:busy.append((key,value,message))
            ),
        ),
    )
    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="cam",
    )

    session.feedback.acquire("sec_0","cam")
    request = dispatcher.pending[-1][0]
    assert request.active
    assert busy[-1][1] is True

    dispatcher.complete(measurement)
    assert busy[-1][1] is False
    assert runtime.get_section_feedback_inspection("sec_0").measurement is not None

    session.feedback.acquire("sec_0","cam")
    request = dispatcher.pending[-1][0]
    session.feedback.prepare_runtime_change()
    assert request.cancelled


def test_automatic_feedback_capability_belongs_to_application_service():
    runtime = _runtime()
    dispatcher = _Dispatcher()
    session = SLMSession(
        runtime=runtime,
        measurement_dispatcher=dispatcher,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(upload_frame=lambda _frame:None),
        ),
    )

    assert session.can_run_automatic_feedback
    session.set_auto_upload_frame(False)
    assert not session.can_run_automatic_feedback
    assert "auto_upload_frame=False" in (
        session.feedback.automatic_feedback_unavailable_reason
    )

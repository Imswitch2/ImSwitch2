from slmcore.application.feedback import (
    AutomaticFeedbackRunner,SLMFeedbackCallbacks,
)
from slmcore.core.cgh import CGHResultState


class _CghStatus:
    result_state = CGHResultState.CURRENT


class _FeedbackStatus:
    def __init__(self,*,stale=False,reason=""):
        self.position_stale = bool(stale)
        self.position_stale_reason = str(reason)


class _Runtime:
    def __init__(self):
        self.feedback_status = _FeedbackStatus()

    def get_section_cgh_status(self,section_key):
        return _CghStatus()

    def get_section_feedback_status(self,section_key):
        return self.feedback_status


class _Session:
    def __init__(self):
        self.runtime = _Runtime()
        self.cgh_callback = None
        self.last_upload_error = None

    def is_cgh_computing(self,section_key):
        return False

    def compute_adapted_cgh(self,section_key,*,on_finished=None):
        self.cgh_callback = on_finished
        return True


class _Service:
    can_run_automatic_feedback = True
    automatic_feedback_unavailable_reason = ""

    def __init__(self,*,reuse_fails=False,previous_available=True):
        self.application_session = _Session()
        self.reuse_fails = reuse_fails
        self.previous_available = previous_available
        self.acquisitions = []
        self.cancel_count = 0
        self.commit_count = 0
        self.reuse_count = 0
        self.localize_count = 0
        self.localize_infer_limits = []
        self.adapt_count = 0
        self.errors = []
        self.warnings = []
        self.states = []
        self.finished = []
        self._callbacks = SLMFeedbackCallbacks(
            on_automatic_state_changed=self.states.append,
            on_automatic_finished=lambda key,text:self.finished.append((key,text)),
        )

    def _error(self,title,error):
        self.errors.append((title,error))

    def _warning(self,title,message):
        self.warnings.append((title,message))

    def feedback_measurement_metadata(self,section_key):
        return {}

    def request_measurement(
        self,section_key,source,*,metadata,on_result,on_error,
    ):
        self.acquisitions.append((on_result,on_error))

    def cancel_measurement(self,section_key):
        self.cancel_count += 1

    def commit_measurement(self,section_key,measurement,**kwargs):
        self.commit_count += 1
        return self.previous_available

    def reuse_localization(self,section_key,*,raise_errors=False):
        self.reuse_count += 1
        if self.reuse_fails:
            raise RuntimeError("incompatible")
        return object()

    def localize_and_commit(self,section_key,*,infer_missing_fraction=None):
        self.localize_count += 1
        self.localize_infer_limits.append(infer_missing_fraction)

    def apply_intensity_feedback(self,section_key,*,raise_errors=False):
        self.adapt_count += 1
        return True,object()


def _runner(service):
    session = service.application_session
    return AutomaticFeedbackRunner(
        runtime_provider=lambda:session.runtime,
        can_run=lambda:bool(service.can_run_automatic_feedback),
        unavailable_reason=lambda:str(service.automatic_feedback_unavailable_reason),
        is_cgh_computing=session.is_cgh_computing,
        compute_adapted_cgh=session.compute_adapted_cgh,
        last_upload_error=lambda:session.last_upload_error,
        callbacks_provider=lambda:service._callbacks,
        request_measurement=service.request_measurement,
        cancel_measurement=service.cancel_measurement,
        feedback_measurement_metadata=service.feedback_measurement_metadata,
        commit_measurement=service.commit_measurement,
        reuse_localization=service.reuse_localization,
        localize_and_commit=service.localize_and_commit,
        apply_intensity_feedback=service.apply_intensity_feedback,
        report_error=service._error,
        report_warning=service._warning,
    )


def test_reuse_failure_stops_without_falling_back_to_localization():
    service = _Service(reuse_fails=True)
    runner = _runner(service)

    assert runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=True,
    )
    assert len(service.acquisitions) == 1

    on_result,_ = service.acquisitions[0]
    on_result(object())

    assert not runner.active
    assert service.commit_count == 1
    assert service.reuse_count == 1
    assert service.localize_count == 0
    assert service.adapt_count == 0
    assert service.errors
    assert "could not be reused" in str(service.errors[-1][1])


def test_reuse_mode_never_runs_fresh_localization_across_rounds():
    service = _Service()
    runner = _runner(service)

    assert runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=True,
    )
    for round_index in range(3):
        service.acquisitions[round_index][0](object())
        assert service.reuse_count == round_index+1
        assert service.localize_count == 0
        service.application_session.cgh_callback(True,None)

    assert not runner.active
    assert service.reuse_count == 3
    assert service.localize_count == 0
    assert service.errors == []
    assert service.finished[-1][1] == "Automatic feedback completed (3 round(s))."


def test_reuse_mode_requires_previous_localization():
    service = _Service(previous_available=False)
    runner = _runner(service)

    assert runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=True,
    )
    service.acquisitions[0][0](object())

    assert not runner.active
    assert service.reuse_count == 0
    assert service.localize_count == 0
    assert service.errors
    assert "no previous localization" in str(service.errors[-1][1]).lower()


def test_fresh_automatic_localization_uses_ten_percent_inference_limit():
    service = _Service()
    runner = _runner(service)

    assert runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=False,
    )
    service.acquisitions[0][0](object())

    assert service.reuse_count == 0
    assert service.localize_count == 1
    assert service.localize_infer_limits == [0.10]
    assert service.adapt_count == 1

    service.application_session.cgh_callback(True,None)
    assert not runner.active
    assert len(service.acquisitions) == 1
    assert service.errors == []


def test_stop_during_acquisition_cancels_immediately():
    service = _Service()
    runner = _runner(service)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )

    runner.stop()

    assert not runner.active
    assert service.cancel_count >= 1
    assert service.finished[-1][1] == "Automatic feedback stopped."


def test_stop_during_cgh_waits_for_that_cgh_then_stops():
    service = _Service()
    runner = _runner(service)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )
    service.acquisitions[0][0](object())

    runner.stop()
    assert runner.active
    assert runner.state.stop_requested
    assert len(service.acquisitions) == 1

    service.application_session.cgh_callback(True,None)

    assert not runner.active
    assert len(service.acquisitions) == 1


def test_stale_position_correction_blocks_automatic_feedback_start():
    service = _Service()
    service.application_session.runtime.feedback_status = _FeedbackStatus(
        stale=True,reason="Position correction is stale because the FOV mapping correction changed.",
    )
    runner = _runner(service)

    assert not runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=False,
    )
    assert not runner.active
    assert service.acquisitions == []
    assert service.errors
    assert "FOV mapping correction changed" in str(service.errors[-1][1])

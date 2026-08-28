from slmcore.application.feedback import (
    AutomaticFeedbackRunner,SLMFeedbackCallbacks,
)
from slmcore.cgh import CGHResultState


class _CghStatus:
    result_state = CGHResultState.CURRENT


class _Runtime:
    def get_section_cgh_status(self,section_key):
        return _CghStatus()


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

    def __init__(self,*,reuse_fails=False):
        self.session = _Session()
        self.reuse_fails = reuse_fails
        self.acquisitions = []
        self.cancel_count = 0
        self.commit_count = 0
        self.reuse_count = 0
        self.localize_count = 0
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
        return True

    def reuse_localization(self,section_key,*,raise_errors=False):
        self.reuse_count += 1
        if self.reuse_fails:
            raise RuntimeError("incompatible")
        return object()

    def localize_and_commit(self,section_key):
        self.localize_count += 1

    def apply_intensity_feedback(self,section_key,*,raise_errors=False):
        self.adapt_count += 1
        return True,object()


def test_one_requested_round_does_not_acquire_an_extra_measurement():
    service = _Service(reuse_fails=True)
    runner = AutomaticFeedbackRunner(service)

    assert runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=True,
    )
    assert len(service.acquisitions) == 1

    on_result,_ = service.acquisitions[0]
    on_result(object())
    assert service.commit_count == 1
    assert service.reuse_count == 1
    assert service.localize_count == 1
    assert service.adapt_count == 1

    service.session.cgh_callback(True,None)

    assert not runner.active
    assert len(service.acquisitions) == 1
    assert service.errors == []
    assert service.finished[-1][1] == "Automatic feedback completed (1 round(s))."


def test_stop_during_acquisition_cancels_immediately():
    service = _Service()
    runner = AutomaticFeedbackRunner(service)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )

    runner.stop()

    assert not runner.active
    assert service.cancel_count >= 1
    assert service.finished[-1][1] == "Automatic feedback stopped."


def test_stop_during_cgh_waits_for_that_cgh_then_stops():
    service = _Service()
    runner = AutomaticFeedbackRunner(service)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )
    service.acquisitions[0][0](object())

    runner.stop()
    assert runner.active
    assert runner.state.stop_requested
    assert len(service.acquisitions) == 1

    service.session.cgh_callback(True,None)

    assert not runner.active
    assert len(service.acquisitions) == 1

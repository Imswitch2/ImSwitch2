import pytest

pytest.importorskip("qtpy")
try:
    from qtpy import QtCore  # noqa: F401
except Exception as error:
    pytest.skip(f"Qt bindings are unavailable: {error}",allow_module_level=True)

from slmcore.cgh import CGHResultState
from slmcore.qt.application.feedback import AutomaticFeedbackRunner


class _CghStatus:
    result_state = CGHResultState.CURRENT


class _Runtime:
    def get_section_cgh_status(self,section_key):
        return _CghStatus()


class _Controller:
    can_run_automatic_feedback = True

    def __init__(self):
        self.runtime = _Runtime()
        self.cgh_callback = None
        self.cancelled_cgh = False

    def flush_section(self,section_key,propagate=True):
        return None

    def is_cgh_computing(self,section_key):
        return False

    def compute_adapted_cgh(self,section_key,*,on_finished=None):
        self.cgh_callback = on_finished
        return True


class _Window:
    class _MeasurementView:
        def set_measurement_status(self,text):
            pass
    measurement_view = _MeasurementView()


class _Coordinator:
    def __init__(self,*,reuse_fails=False):
        self.controller = _Controller()
        self.reuse_fails = reuse_fails
        self.acquisitions = []
        self.cancel_count = 0
        self.commit_count = 0
        self.reuse_count = 0
        self.localize_count = 0
        self.adapt_count = 0
        self.states = []
        self.errors = []
        self.warnings = []
        self._window = _Window()

    def _error(self,title,error):
        self.errors.append((title,error))

    def _warning(self,title,message):
        self.warnings.append((title,message))

    def _set_automatic_operation(self,active,**kwargs):
        self.states.append((active,kwargs))

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

    def window(self,section_key):
        return self._window


def test_one_requested_round_does_not_acquire_an_extra_measurement():
    coordinator = _Coordinator(reuse_fails=True)
    runner = AutomaticFeedbackRunner(coordinator)

    assert runner.start(
        "sec_0",rounds=1,source="cam",reuse_previous_localization=True,
    )
    assert len(coordinator.acquisitions) == 1

    on_result,_ = coordinator.acquisitions[0]
    on_result(object())
    assert coordinator.commit_count == 1
    assert coordinator.reuse_count == 1
    assert coordinator.localize_count == 1
    assert coordinator.adapt_count == 1

    coordinator.controller.cgh_callback(True,None)

    assert not runner.active
    assert len(coordinator.acquisitions) == 1
    assert coordinator.errors == []


def test_stop_during_acquisition_cancels_immediately():
    coordinator = _Coordinator()
    runner = AutomaticFeedbackRunner(coordinator)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )

    runner.stop()

    assert not runner.active
    assert coordinator.cancel_count >= 1


def test_stop_during_cgh_waits_for_that_cgh_then_stops():
    coordinator = _Coordinator()
    runner = AutomaticFeedbackRunner(coordinator)
    runner.start(
        "sec_0",rounds=3,source="cam",reuse_previous_localization=False,
    )
    coordinator.acquisitions[0][0](object())

    runner.stop()
    assert runner.active
    assert len(coordinator.acquisitions) == 1

    coordinator.controller.cgh_callback(True,None)

    assert not runner.active
    assert len(coordinator.acquisitions) == 1

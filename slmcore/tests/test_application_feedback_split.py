"""Checks for the responsibility-based application feedback module layout."""

from slmcore.application import SLMFeedbackService as application_feedback_service
from slmcore.application.feedback import AutomaticFeedbackRunner,SLMFeedbackService
from slmcore.application.feedback.adaptation import FeedbackAdaptationService
from slmcore.application.feedback.fov_calibration import FOVPositionCalibrationService
from slmcore.application.feedback.geometry_calibration import GeometryOrientationFeedbackService
from slmcore.application.feedback.localization import LocalizationService
from slmcore.application.feedback.measurement import MeasurementService
from slmcore.application.feedback.references import PositionReferenceService
from slmcore.application.feedback.service import SLMFeedbackService as facade_service


def test_feedback_public_api_is_owned_by_facade_module():
    assert SLMFeedbackService is facade_service
    assert application_feedback_service is facade_service


def test_feedback_workflows_have_focused_application_services():
    services = (
        MeasurementService,
        LocalizationService,
        PositionReferenceService,
        FOVPositionCalibrationService,
        GeometryOrientationFeedbackService,
        FeedbackAdaptationService,
    )
    assert len({service.__module__ for service in services}) == len(services)


def test_focused_feedback_services_do_not_retain_owner_locators():
    import inspect

    focused = (
        AutomaticFeedbackRunner,
        MeasurementService,
        LocalizationService,
        PositionReferenceService,
        FOVPositionCalibrationService,
        GeometryOrientationFeedbackService,
        FeedbackAdaptationService,
    )
    forbidden = ("self._feedback =", "self._service =", "self._application_session =")
    for service in focused:
        source = inspect.getsource(service)
        for token in forbidden:
            assert token not in source, f"{service.__name__} retains owner locator {token}"


def test_feedback_fov_runtime_service_has_no_calibration_producer_path():
    import inspect

    source = inspect.getsource(FOVPositionCalibrationService)
    obsolete = (
        "fit_fov_position_calibration",
        "save_fov_position_calibration",
        "replace_applied_fov_position_calibration",
        "fov_position_calibration_replacement_warning",
        "_fov_position_calibration_candidates",
    )
    for token in obsolete:
        assert token not in source


def test_transitional_fov_position_calibration_alias_is_removed():
    import slmcore
    import slmcore.core.cgh.feedback as feedback_core

    assert not hasattr(slmcore,"FOVPositionCalibration")
    assert not hasattr(feedback_core,"FOVPositionCalibration")

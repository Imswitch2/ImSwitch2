"""Checks for the responsibility-based application feedback module layout."""

from slmcore.application import SLMFeedbackService as application_feedback_service
from slmcore.application.feedback import SLMFeedbackService
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

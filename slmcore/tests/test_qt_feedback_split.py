"""Checks for the responsibility-based Qt feedback coordinator layout."""

import inspect

import pytest

pytest.importorskip("qtpy")

from slmcore.qt.application.feedback import FeedbackCoordinator
from slmcore.qt.application.feedback.actions import FeedbackActionRouter
from slmcore.qt.application.feedback.adaptation import FeedbackAdaptationCoordinator
from slmcore.qt.application.feedback.automatic import AutomaticFeedbackCoordinator
from slmcore.qt.application.feedback.localization import LocalizationCoordinator
from slmcore.qt.application.feedback.measurement import MeasurementCoordinator
from slmcore.qt.application.feedback.references import PositionReferenceCoordinator


def test_feedback_ui_workflows_have_focused_qt_coordinators():
    coordinators = (
        MeasurementCoordinator,
        LocalizationCoordinator,
        PositionReferenceCoordinator,
        FeedbackAdaptationCoordinator,
        AutomaticFeedbackCoordinator,
        FeedbackActionRouter,
    )
    assert len({coordinator.__module__ for coordinator in coordinators}) == len(coordinators)


def test_only_top_level_feedback_coordinator_depends_on_qt_session():
    assert "_qt_session" in inspect.getsource(FeedbackCoordinator)
    focused = (
        MeasurementCoordinator,
        LocalizationCoordinator,
        PositionReferenceCoordinator,
        FeedbackAdaptationCoordinator,
        AutomaticFeedbackCoordinator,
        FeedbackActionRouter,
    )
    for coordinator in focused:
        assert "_qt_session" not in inspect.getsource(coordinator)

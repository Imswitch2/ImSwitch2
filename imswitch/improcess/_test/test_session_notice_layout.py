"""Regression: long reconstruction errors cannot overlap the Session name."""

import pytest

pytest.importorskip("qtpy")

from imswitch.improcess.view.ProcessingSessionFrame import ProcessingSessionFrame


def test_session_error_occupies_dedicated_row_and_remains_dismissible(qt_app):
    frame = ProcessingSessionFrame()
    frame.setSessionActive(True, "a very long acquisition session name")
    message = "Reconstruction failed: selected timelapse has missing T3; " + "details " * 35
    frame.setStatus(message)

    assert frame.statusRow.isHidden() is False
    assert frame.statusLabel.fullText() == message
    assert frame.statusLabel.toolTip() == message
    assert frame.statusLabel.parentWidget() is frame.statusRow
    assert frame.sessionNameLabel.parentWidget() is frame
    frame._dismissStatus()
    assert frame.statusRow.isHidden()
    frame.close()


def test_queue_failure_surfaces_notice_and_resets_duplicate_tracking(qt_app):
    frame = ProcessingSessionFrame()
    frame.setSessionActive(True, "replay")
    status = {"kind": "error", "label": "T lapse",
              "status": "Reconstruction failed: missing T3", "detail": "missing T3"}
    frame.setProcessingQueue({"last_terminal_status": status})
    assert frame.statusRow.isHidden() is False
    assert "missing T3" in frame.statusLabel.fullText()
    frame.setProcessingQueue({"last_terminal_status": {}})
    frame.close()

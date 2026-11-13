from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.model.timepoint_scope import TimepointScope  # noqa: E402
from imswitch.improcess.view.ImProcessMainView import BtnFrame  # noqa: E402


_APP = None


@pytest.fixture(scope="module")
def qapp():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def test_scope_control_is_only_for_one_logical_lapse_and_snapshots_current_t(qapp):
    footer = BtnFrame()
    identity = ("/tmp/lapse.h5", "CAM")
    footer.setSelectionCount("loaded", 1)
    footer.setTimeLapseSelection("loaded", {"identity": identity, "total": 20})

    assert not footer.scopeCombo.isHidden()
    assert footer.selectedTimepointScope() == TimepointScope.all()

    footer.setCurrentTimeLapseContext({
        "identity": identity,
        "position": 6,
        "total": 20,
        "ready": True,
    })
    footer.scopeCombo.setCurrentIndex(1)
    assert footer.selectedTimepointScope() == TimepointScope.single(6)

    # Lazy loading invalidates Current T until the requested point is materialized.
    footer.setCurrentTimeLapseContext({
        "identity": identity,
        "position": 6,
        "total": 20,
        "ready": False,
    })
    assert footer.selectedTimepointScope() is None
    assert not footer.processSelectedBtn.isEnabled()

    # A different/ordinary selection hides scope and resets to whole-source semantics.
    footer.setTimeLapseSelection("loaded", None)
    assert footer.scopeCombo.isHidden()
    assert footer.selectedTimepointScope() == TimepointScope.all()
    footer.deleteLater()


def test_scope_choice_is_kept_across_selection_and_workspace_changes(qapp):
    footer = BtnFrame()
    first = ("/tmp/first.zarr", "CAM")
    second = ("/tmp/second.zarr", "CAM")
    footer.setTimeLapseSelection("loaded", {"identity": first, "total": 20})
    footer.setCurrentTimeLapseContext({
        "identity": first, "position": 4, "total": 20, "ready": True,
    })
    footer.scopeCombo.setCurrentIndex(1)  # Current T
    assert footer.selectedTimepointScope() == TimepointScope.single(4)

    footer.setTimeLapseSelection("loaded", {"identity": second, "total": 12})
    footer.setCurrentTimeLapseContext({
        "identity": second, "position": 7, "total": 12, "ready": True,
    })
    assert footer.scopeCombo.currentData() == "current"
    assert footer.selectedTimepointScope() == TimepointScope.single(7)

    footer.setContext("session")
    footer.setContext("loaded")
    assert footer.scopeCombo.currentData() == "current"
    assert footer.selectedTimepointScope() == TimepointScope.single(7)
    footer.deleteLater()


def test_range_choice_is_remembered_and_bounded_for_smaller_lapses(qapp):
    footer = BtnFrame()
    footer.setTimeLapseSelection("loaded", {"identity": ("long", "CAM"), "total": 20})
    # Set the committed state directly instead of opening a modal dialog.
    footer._timepointScopeMode = "range"
    footer._timepointScopeRange = (3, 14)
    with QtCore.QSignalBlocker(footer.scopeCombo):
        footer.scopeCombo.setCurrentIndex(2)
    footer._syncScopeVisibility()
    assert footer.selectedTimepointScope() == TimepointScope.range(3, 14)

    footer.setTimeLapseSelection("loaded", {"identity": ("short", "CAM"), "total": 6})
    assert footer.scopeCombo.currentData() == "range"
    assert footer.selectedTimepointScope() == TimepointScope.range(3, 5)
    # Changing back to a longer lapse restores the original requested range.
    footer.setTimeLapseSelection("loaded", {"identity": ("long", "CAM"), "total": 20})
    assert footer.selectedTimepointScope() == TimepointScope.range(3, 14)
    footer.deleteLater()


def test_monitor_auto_live_can_be_disabled_before_first_arrival(qapp):
    footer = BtnFrame()
    footer.setContext("session")
    footer.setSessionActive(True)
    footer.setAutoProcessingAvailable(True)
    footer.setSessionStreamingCapability("MoNaLISA · Fast", True, "")

    footer.setProcessingQueue({
        "auto_enabled": False, "process_while_acquiring": False,
    })
    assert not footer.processWhileAcquiringCheck.isEnabled()

    # Monitor has just been enabled. The queue arms Auto and defaults Live on,
    # but no acquisition has arrived or reconstruction has run yet.
    footer.setProcessingQueue({
        "auto_enabled": True, "process_while_acquiring": True,
    })
    assert footer.autoProcessCheck.isChecked()
    assert footer.processWhileAcquiringCheck.isChecked()
    assert footer.processWhileAcquiringCheck.isEnabled()

    changes = []
    footer.sigProcessWhileAcquiringChanged.connect(changes.append)
    footer.processWhileAcquiringCheck.click()
    assert changes == [False]
    assert not footer.processWhileAcquiringCheck.isChecked()
    footer.deleteLater()

def test_live_remains_disabled_if_selected_method_cannot_stream(qapp):
    footer = BtnFrame()
    footer.setContext("session")
    footer.setSessionActive(True)
    footer.setAutoProcessingAvailable(True)
    footer.setSessionStreamingCapability("MoNaLISA · Robust", False, "Requires finite processing")
    footer.setProcessingQueue({
        "auto_enabled": True, "process_while_acquiring": False,
    })
    assert footer.autoProcessCheck.isEnabled()
    assert footer.autoProcessCheck.isChecked()
    assert not footer.processWhileAcquiringCheck.isEnabled()
    footer.deleteLater()

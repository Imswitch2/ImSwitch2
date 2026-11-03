from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

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

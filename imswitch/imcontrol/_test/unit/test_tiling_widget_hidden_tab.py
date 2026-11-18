"""The Tiling panel's choices hold while the panel is a tab behind another.

Found on the rig (2026-11-18): the Tiling and Timelapse panels shared a dock,
and a tiling timelapse started from the Timelapse tab aligned on the setup's
default detector (a scan-driven APD) instead of the Widefield camera chosen in
the Tiling panel -- the getters asked Qt whether the combo was *visible*,
which a background tab's widgets never are.
"""

import pytest

pytest.importorskip("PyQt5")

from qtpy import QtWidgets  # noqa: E402

from imswitch.imcontrol.view.widgets.TilingWidget import TilingWidget  # noqa: E402


@pytest.fixture
def behindAnotherTab(qtbot):
    tabs = QtWidgets.QTabWidget()
    qtbot.addWidget(tabs)
    tabs.addTab(QtWidgets.QLabel('Timelapse'), 'Timelapse')
    tiling = TilingWidget(None)
    tabs.addTab(tiling, 'Tiling')
    tabs.setCurrentIndex(0)
    tabs.show()
    qtbot.waitExposed(tabs)
    assert not tiling.isVisible(), 'the Tiling tab is in the background'
    # Keep the tab widget alive: it owns the panel's Qt objects.
    tiling._testTabs = tabs
    return tiling


def test_the_chosen_detector_is_returned_from_a_background_tab(behindAnotherTab):
    tiling = behindAnotherTab
    tiling.setDetectors(['APDgreen', 'Widefield'])
    tiling.setDetector('Widefield')
    assert tiling.getDetector() == 'Widefield'


def test_the_chosen_scan_source_is_returned_from_a_background_tab(behindAnotherTab):
    tiling = behindAnotherTab
    tiling.setScanSources(['Scan', 'TriggerScopeScan'])
    tiling.setScanSource('TriggerScopeScan')
    assert tiling.getScanSource() == 'TriggerScopeScan'


def test_no_choice_offered_still_leaves_it_to_the_setup(behindAnotherTab):
    tiling = behindAnotherTab
    tiling.setDetectors(['Widefield'])
    tiling.setScanSources(['Scan'])
    assert tiling.getDetector() is None
    assert tiling.getScanSource() is None


def test_a_panel_never_shown_returns_the_choice_too(qtbot):
    tiling = TilingWidget(None)
    qtbot.addWidget(tiling)
    tiling.setDetectors(['APDgreen', 'Widefield'])
    tiling.setDetector('Widefield')
    assert tiling.getDetector() == 'Widefield'

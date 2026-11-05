"""Non-modal Beta timing notices remain in their respective scan pages."""

import pytest

pytest.importorskip('PyQt5')


def test_advanced_shows_timing_warning_at_dwell_control(qtbot):
    from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced

    widget = ScanWidgetAdvanced(None)
    qtbot.addWidget(widget)
    widget.initControls(['X', 'Y'], ['Laser'], 'ms')
    widget.show()

    widget.setStageTimingWarning('Stage timing uses defaults', 'Not calibrated')
    assert widget.stageTimingWarningLabel.isVisible()
    assert 'Stage timing uses defaults' in widget.stageTimingWarningLabel.text()
    assert widget.stageTimingWarningLabel.toolTip() == 'Not calibrated'
    widget.setStageTimingWarning()
    assert not widget.stageTimingWarningLabel.isVisible()


def test_simple_monalisa_warning_is_embedded_in_scan_group(qtbot):
    from imswitch.imcontrol.view.widgets.ScanWidgetSimpleMoNaLISA import MoNaLISAView

    view = MoNaLISAView()
    qtbot.addWidget(view)
    view.show()
    view.setStageTimingWarning('Only 1 ms stationary', 'Stage moves for 4 ms')
    assert view.stageTimingWarningLabel.isVisible()
    assert 'Only 1 ms stationary' in view.stageTimingWarningLabel.text()
    view.setStageTimingWarning()
    assert not view.stageTimingWarningLabel.isVisible()

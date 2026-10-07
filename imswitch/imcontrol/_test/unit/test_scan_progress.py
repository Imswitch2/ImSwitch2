"""Advanced scan progress bar: duration, text, widget and run tracking."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
    scan_duration_s,
    scan_progress,
)


def test_duration_prefers_the_designers_total_scan_time():
    assert scan_duration_s({'tot_scan_time_s': 12.5}) == 12.5


def test_duration_falls_back_to_samples_times_sample_period():
    # The contract's default tot_scan_time_s is 0 (Beta designer).
    info = {'tot_scan_time_s': 0.0, 'scan_samples_total': 2000,
            'scan_time_step': 1e-5}
    assert scan_duration_s(info) == pytest.approx(0.02)


@pytest.mark.parametrize('info', [None, {}, {'tot_scan_time_s': 'x'}])
def test_duration_unknown(info):
    assert scan_duration_s(info) is None


def test_progress_text():
    assert scan_progress(30, 100, 1, False) == (0.3, '30 s / 1:40')
    assert scan_progress(30, 100, 2, True) == (0.3, 'Frame 2 · 30 s / 1:40')
    assert scan_progress(120, 100, 1, False) == (1.0, 'finishing…')
    assert scan_progress(5, None, 1, False) == (None, '5 s')
    assert scan_progress(3725, 7200, 1, False)[1] == '1:02:05 / 2:00:00'


class _Coordinator:
    def __init__(self):
        self.run = None
        self.token = None

    def runForOwner(self, owner):
        return self.run

    def tokenForOwner(self, owner):
        return self.token


def _controller():
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    ctrl._scanCoordinator = _Coordinator()
    ctrl._widget = MagicMock()
    ctrl._widget.repeatEnabled.return_value = False
    ctrl._progressTimer = MagicMock()
    ctrl._progressRun = None
    ctrl._progressFrame = 0
    ctrl._progressStarted = None
    ctrl._progressDuration = None
    ctrl.scanInfoDict = None
    return ctrl


def test_progress_follows_the_owned_run_and_hides_when_it_is_released():
    ctrl = _controller()
    coordinator = ctrl._scanCoordinator

    # Another controller's iteration: nothing shown.
    ctrl._onScanIterationStarted()
    ctrl._widget.showScanProgress.assert_not_called()

    coordinator.run = object()
    coordinator.token = SimpleNamespace(scanInfoDict={'tot_scan_time_s': 10.0})
    ctrl._onScanIterationStarted()
    fraction, text = ctrl._widget.showScanProgress.call_args.args
    assert 0 <= fraction < 0.1 and text.endswith('/ 10 s')
    ctrl._progressTimer.start.assert_called_once()

    # A repeat frame of the same run counts up.
    ctrl._onScanIterationStarted()
    assert ctrl._widget.showScanProgress.call_args.args[1].startswith('Frame 2')

    # Run released (done, failed or aborted): the tick hides and stops.
    coordinator.run = None
    coordinator.token = None
    ctrl._updateScanProgress()
    ctrl._widget.hideScanProgress.assert_called_once()
    ctrl._progressTimer.stop.assert_called_once()

    # A new run starts counting from frame 1 again.
    coordinator.run = object()
    coordinator.token = SimpleNamespace(scanInfoDict={'tot_scan_time_s': 10.0})
    ctrl._onScanIterationStarted()
    assert not ctrl._widget.showScanProgress.call_args.args[1].startswith('Frame')


def test_widget_progress_row_is_optional_and_on_by_default(qtbot):
    from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import (
        ScanWidgetAdvanced,
    )

    widget = ScanWidgetAdvanced(None)
    qtbot.addWidget(widget)
    widget.initControls(['X', 'Y'], ['Laser'], 'ms')
    widget.show()

    assert widget.progressEnabled()
    assert not widget.scanProgressRow.isVisible()  # idle

    widget.showScanProgress(0.25, '25 s / 1:40')
    assert widget.scanProgressRow.isVisible()
    assert widget.scanProgressBar.value() == 250
    assert widget.scanProgressLabel.text() == '25 s / 1:40'

    widget.setProgressEnabled(False)
    assert not widget.scanProgressRow.isVisible()
    widget.setProgressEnabled(True)
    assert widget.scanProgressRow.isVisible()

    widget.showScanProgress(None, '5 s')
    assert widget.scanProgressBar.maximum() == 0  # busy indicator

    widget.hideScanProgress()
    assert not widget.scanProgressRow.isVisible()

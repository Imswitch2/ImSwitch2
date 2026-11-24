"""A TriggerScope scan start no longer blocks, so a second start can arrive.

The upload of a scan's parameters runs on the uploader thread; the start slot
returns while it is under way. While it is, the controller must ignore another
start request (the button is already disabled, but the API and Repeat paths do
not go through the button), a force-stop must keep the rest of the upload and
the scan command from being sent, a failed upload must end the run as a visible
refusal, and a board that never answers must not hold the run forever.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol._test.unit.test_triggerscope_scan_lifecycle import (
    _CommChannel, _Controller, _DetectorsManager, _Detector, _Signal,
)
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _AsyncScanManager:
    """runScanAsync returns at once; the test decides when the board is done."""

    def __init__(self, events):
        self.events = events
        self.sigScanStarted = _Signal('board-started', events)
        self.sigScanDone = _Signal('board-done', events)
        self.calls = []
        self.handles = []
        self.callbacks = []
        self.syncCalls = []
        self.error = None

    def runScanAsync(self, parameters, scan_type, onFinished):
        self.calls.append((parameters, scan_type))
        if self.error is not None:
            raise self.error
        handle = _Handle()
        self.handles.append(handle)
        self.callbacks.append(onFinished)
        return handle

    def runScan(self, parameters, scan_type):
        self.syncCalls.append((parameters, scan_type))


def _setup():
    events = []
    detector = _Detector()
    detectors = _DetectorsManager(events, detector)
    scanManager = _AsyncScanManager(events)
    master = SimpleNamespace(
        detectorsManager=detectors,
        nidaqManager=SimpleNamespace(),
        scanManager=scanManager,
        scanExecutionCoordinator=ScanExecutionCoordinator(
            detectors, SimpleNamespace()),
    )
    channel = _CommChannel(events)
    channel.sigScanRequestRejected = _Signal('rejected', events)
    controller = _Controller(master, channel)
    return controller, scanManager, channel, events


def _start(controller):
    return controller.runScanAdvanced(sigScanStartingEmitted=False)


def _names(events):
    return [e[0] for e in events]


def test_the_start_returns_before_the_board_has_the_scan():
    controller, scanManager, channel, events = _setup()

    assert _start(controller) is True

    assert controller.isRunning
    assert len(scanManager.calls) == 1
    assert scanManager.syncCalls == []
    assert 'scan-ended' not in _names(events)


def test_a_second_start_while_the_upload_is_in_flight_is_ignored():
    controller, scanManager, channel, events = _setup()
    _start(controller)

    assert _start(controller) is False
    assert _start(controller) is False

    assert len(scanManager.calls) == 1
    reasons = [a[0] for n, a in events if n == 'rejected']
    assert reasons.count(
        'Ignoring a TriggerScope scan start while the previous start is '
        'still being sent to the board.') == 2
    assert controller.isRunning                       # the first start is intact
    assert controller._widget.scanButtonChecked is True


def test_a_finished_upload_clears_the_guard_and_the_run_proceeds_to_scan_done():
    controller, scanManager, channel, events = _setup()
    _start(controller)

    scanManager.callbacks[0](True, '')
    assert controller._triggerScopeStartInFlight is None
    scanManager.sigScanDone.emit()

    assert 'scan-done' in _names(events)
    assert 'scan-ended' in _names(events)
    # and the next start is accepted again
    assert _start(controller) is True
    assert len(scanManager.calls) == 2


def test_a_failed_upload_ends_the_run_and_says_why():
    controller, scanManager, channel, events = _setup()
    _start(controller)

    scanManager.callbacks[0](False, 'OSError: port went away')

    assert not controller.isRunning
    assert controller._widget.scanButtonChecked is False
    assert 'scan-ended' in _names(events)
    rejected = [a for n, a in events if n == 'rejected']
    assert rejected and 'port went away' in rejected[0][0]
    assert any(level == 'error' and 'port went away' in text
               for level, text in controller._logger.messages)
    # the start button is usable again
    assert _start(controller) is True


def test_a_force_stop_during_the_upload_cancels_it():
    controller, scanManager, channel, events = _setup()
    _start(controller)
    handle = scanManager.handles[0]

    controller._requestTriggerScopeStop()          # "Stop after scan"
    assert not handle.cancelled                    # the iteration still runs
    controller._failTriggerScopeScan()             # "Force stop / disarm"

    assert handle.cancelled
    assert controller._triggerScopeStartInFlight is None
    assert 'scan-ended' in _names(events)
    # the late completion of the cancelled upload changes nothing
    ended = _names(events).count('scan-ended')
    scanManager.callbacks[0](False, 'cancelled before all commands were sent')
    assert _names(events).count('scan-ended') == ended


def test_an_upload_that_never_finishes_is_given_up_after_the_timeout():
    controller, scanManager, channel, events = _setup()
    _start(controller)
    handle = scanManager.handles[0]
    token = controller._scanCoordinator.tokenForOwner(controller)

    controller._onTriggerScopeUploadTimeout(token, handle)

    assert handle.cancelled
    assert not controller.isRunning
    assert 'scan-ended' in _names(events)
    rejected = [a for n, a in events if n == 'rejected']
    assert rejected and 'no answer from the board' in rejected[0][0]


def test_a_timeout_after_the_upload_finished_does_nothing():
    controller, scanManager, channel, events = _setup()
    _start(controller)
    handle = scanManager.handles[0]
    token = controller._scanCoordinator.tokenForOwner(controller)
    scanManager.callbacks[0](True, '')

    controller._onTriggerScopeUploadTimeout(token, handle)

    assert not handle.cancelled
    assert controller.isRunning


def test_a_scan_done_that_overtakes_the_upload_report_does_not_wedge_the_guard():
    """The board can answer before the uploader's completion is delivered."""
    controller, scanManager, channel, events = _setup()
    _start(controller)

    scanManager.sigScanDone.emit()
    scanManager.callbacks[0](True, '')

    assert controller._triggerScopeStartInFlight is None
    assert _start(controller) is True


def test_a_configuration_error_surfaces_synchronously_as_a_failed_start():
    controller, scanManager, channel, events = _setup()
    scanManager.error = ValueError('device has no TriggerScope TTL line')

    # the real controllers' runScanAdvanced catches this and calls scanFailed
    with pytest.raises(ValueError, match='no TriggerScope TTL line'):
        _start(controller)

    assert controller._triggerScopeStartInFlight is None
    assert controller._scanCoordinator.tokenForOwner(controller) is None

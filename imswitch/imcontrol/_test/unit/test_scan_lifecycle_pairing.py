"""Every published scan end must have had a start.

``runScanExternal`` arms with ``sigScanStartingEmitted=True``: it asserts the
run-level start is already on the channel, and on that basis its terminal
publishes ``sigScanEnded``. So a dispatcher that skips the start does not
produce a quiet scan -- it produces one that *ends* without ever having begun.

The consumer that pays for this is the focus lock. It yields the focus axis on
``sigScanStarting`` and takes it back on ``sigScanEnded``, depth-counted; an
unpaired end drives the depth to zero and it resumes correcting into a live
waveform. On a tiling run of a hundred XYZ tiles that is a hundred scans fought
plane by plane -- which is exactly how this was found.
"""

from types import SimpleNamespace

import pytest

import imswitch.imcontrol.controller.basecontrollers  # noqa: F401  (sip shim)
from imswitch.imcontrol.controller.WorkflowServices import (
    ScanRequestResult,
    ScanWorkflowService,
)
from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
)


class _Signal:
    """Enough of a Qt signal to be connected to and emitted synchronously."""

    def __init__(self, log, name):
        self._log = log
        self._name = name
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot):
        self._slots.remove(slot)

    def emit(self, *args):
        self._log.append(self._name)
        for slot in list(self._slots):
            slot(*args)


class _Channel:
    """Records the lifecycle in order."""

    def __init__(self):
        self.lifecycle = []
        self.sigScanStarting = _Signal(self.lifecycle, 'start')
        self.sigScanEnded = _Signal(self.lifecycle, 'end')


def _service(channel, *, report='accept'):
    """A service whose dispatch runs synchronously.

    ``report`` is the distinction the pairing rule turns on: ``'accept'`` (a
    controller owns the terminal), ``'refuse'`` (it reported and declined, so
    nobody does) and ``None`` (a legacy receiver that implements no
    acknowledgement contract and may have started anyway).
    """
    service = ScanWorkflowService.__new__(ScanWorkflowService)
    service.__dict__.update(_comm_channel=channel, _activeScanRequest=None)

    def dispatch(action):
        request = ScanRequestResult()
        service.__dict__['_activeScanRequest'] = request
        if report is not None:
            # Controllers report while the request is being delivered, i.e.
            # before the action returns -- not after it.
            request.report(object(), report == 'accept', 'declined')
        try:
            action()
        finally:
            service.__dict__['_activeScanRequest'] = None
        return request

    service.__dict__['_dispatch_scan_request'] = dispatch
    return service


def _source(channel, onRun=None):
    def runScanExternal(_recalculate, _nonFinal):
        # The scan controller's own terminal publishes the end, on the strength
        # of the start the dispatcher was supposed to have published.
        if onRun is not None:
            onRun()
        channel.sigScanEnded.emit()

    return SimpleNamespace(runScanExternal=runScanExternal)


def test_each_dispatched_scan_publishes_its_own_start():
    channel = _Channel()
    service = _service(channel)
    source = _source(channel)

    for _tile in range(3):
        service.run_scan_from(source, False, False)

    assert channel.lifecycle == ['start', 'end'] * 3


def test_the_start_precedes_the_arm_rather_than_racing_it():
    """A consumer must have yielded before the scan touches hardware."""
    channel = _Channel()
    seen = []
    source = _source(channel, onRun=lambda: seen.append(list(channel.lifecycle)))
    service = _service(channel)

    service.run_scan_from(source, False, False)

    assert seen == [['start']]


def test_a_refused_request_still_pairs_the_start_it_published():
    """Nobody accepted, so no controller owes an end -- the dispatcher does."""
    channel = _Channel()
    service = _service(channel, report='refuse')
    refused = SimpleNamespace(
        runScanExternal=lambda _recalculate, _nonFinal: None
    )

    service.run_scan_from(refused, False, False)

    assert channel.lifecycle == ['start', 'end']


def test_an_unreported_legacy_receiver_keeps_its_own_end():
    """Silence is not a refusal.

    A receiver that implements no acknowledgement contract may still have
    started, and will publish its own end when it finishes. Pairing here would
    race that -- telling every consumer the scan was over while it ran.
    """
    channel = _Channel()
    service = _service(channel, report=None)
    legacy = SimpleNamespace(
        runScanExternal=lambda _recalculate, _nonFinal: None
    )

    service.run_scan_from(legacy, False, False)

    assert channel.lifecycle == ['start']


def test_a_controller_that_ends_within_the_call_is_not_ended_twice():
    """An end observed during dispatch means the terminal already has an owner."""
    channel = _Channel()
    service = _service(channel, report='refuse')
    # Refuses the request, but publishes a terminal anyway -- the shape that
    # would otherwise collect a second end on top of its own.
    endsItself = _source(channel)

    service.run_scan_from(endsItself, False, False)

    assert channel.lifecycle == ['start', 'end']


def test_a_raising_dispatch_still_pairs_the_start_it_published():
    channel = _Channel()
    service = _service(channel, report='refuse')

    def explode(_recalculate, _nonFinal):
        raise RuntimeError('the scan controller refused mid-arm')

    with pytest.raises(RuntimeError):
        service.run_scan_from(
            SimpleNamespace(runScanExternal=explode), False, False
        )

    assert channel.lifecycle == ['start', 'end']


def test_a_caller_that_owns_the_lifecycle_is_not_doubled_up():
    """Recording publishes its own start; a second one could never unwind."""
    channel = _Channel()
    service = _service(channel)
    source = _source(channel)

    service.run_scan_from(source, False, False, notify_starting=False)

    assert channel.lifecycle == ['end']


# ---------------------------------------------------------------------------
# What an unpaired end costs, at the consumer that pays for it
# ---------------------------------------------------------------------------


def _focusLock():
    lock = FocusLockController.__new__(FocusLockController)
    lock.__dict__.update(
        _scanSuspendDepth=0,
        _scanOwnsFocusActuator=True,
        _suspendedLock=False,
        _reacquireFailed=False,
        _preScanSetPoint=None,
        setPointSignal=0.0,
        locked=True,
        aboutToLock=False,
        _lastPIUpdate=None,
        pi=None,
        _widget=SimpleNamespace(
            ScanBlock=SimpleNamespace(isChecked=lambda: True)
        ),
        _clearPendingCorrection=lambda: None,
        _endReacquire=lambda notify: None,
        _cancelCalibrationForScan=lambda: None,
        _publishFocusLockState=lambda: None,
        _beginReacquire=lambda: lock.__dict__.update(locked=True),
        _logger=SimpleNamespace(warning=print, error=print, info=print),
    )
    return lock


def test_paired_lifecycles_keep_the_lock_off_the_axis_for_every_tile():
    lock = _focusLock()

    drivingDuringScan = []
    for _tile in range(4):
        lock.scanUnlockFocus()
        drivingDuringScan.append(lock.locked)
        lock.scanLockFocus()

    assert drivingDuringScan == [False] * 4
    assert lock.locked is True      # holding again between tiles


def test_one_start_for_many_ends_leaves_the_lock_fighting_the_scan():
    """The pre-fix shape, pinned so it cannot come back unnoticed."""
    lock = _focusLock()

    lock.scanUnlockFocus()          # a single run-level start
    drivingDuringScan = []
    for _tile in range(4):
        drivingDuringScan.append(lock.locked)
        lock.scanLockFocus()        # one end per tile

    assert drivingDuringScan == [False, True, True, True]


def test_a_lapse_publishes_a_start_for_every_timepoint_not_only_the_first():
    """Each timepoint runs its own scan, so each owes its own start.

    Checked structurally rather than by driving ``nextLapse``: the defect was
    exactly a placement -- the call sitting inside the first-timepoint block --
    and placement is what this pins.
    """
    import ast
    import inspect
    import textwrap

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(RecordingController.nextLapse))
    )

    def publishesStart(node):
        return any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == '_notifyScanStarting'
            for child in ast.walk(node)
        )

    assert publishesStart(tree), 'the lapse must publish the scan start'
    conditional = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and publishesStart(node)
    ]
    assert not conditional, (
        'the scan start is published under a condition; a lapse point that '
        'skips it still publishes an end, and consumers resume mid-scan'
    )


def test_the_legacy_broadcast_publishes_a_start_as_well():
    """``sigRunScan`` lands on ``runScanExternal`` exactly as targeting does.

    Same assertion, same owed start. The broadcast is the fallback for setups
    without source resolution, so it is the one most likely to be running
    unattended when it goes wrong.
    """
    channel = _Channel()
    channel.sigRunScan = _Signal(channel.lifecycle, 'run')
    channel.sigRunScan.connect(
        lambda *_args: channel.sigScanEnded.emit()   # the receiver's terminal
    )
    service = _service(channel)
    service.__dict__['_resolved_scan_source'] = lambda: None

    service.run_scan(False, False)

    assert channel.lifecycle == ['start', 'run', 'end']


def test_the_broadcast_honours_a_caller_that_owns_the_lifecycle():
    channel = _Channel()
    channel.sigRunScan = _Signal(channel.lifecycle, 'run')
    channel.sigRunScan.connect(lambda *_args: channel.sigScanEnded.emit())
    service = _service(channel)
    service.__dict__['_resolved_scan_source'] = lambda: None

    service.run_scan(False, False, notify_starting=False)

    assert channel.lifecycle == ['run', 'end']

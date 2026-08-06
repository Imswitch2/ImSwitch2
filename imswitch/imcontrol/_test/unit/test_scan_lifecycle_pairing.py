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


class _Channel:
    """Records the lifecycle in order, and dispatches synchronously."""

    def __init__(self):
        self.lifecycle = []
        self.sigScanStarting = SimpleNamespace(
            emit=lambda: self.lifecycle.append('start')
        )
        self.sigScanEnded = SimpleNamespace(
            emit=lambda: self.lifecycle.append('end')
        )


def _service(channel, *, accepted=True, onRun=None):
    service = ScanWorkflowService.__new__(ScanWorkflowService)
    service.__dict__.update(_comm_channel=channel, _activeScanRequest=None)

    def dispatch(action):
        # The real service marshals onto the UI thread; here the point is only
        # that the action runs synchronously and reports through the envelope.
        request = ScanRequestResult()
        service.__dict__['_activeScanRequest'] = request
        try:
            action()
        finally:
            service.__dict__['_activeScanRequest'] = None
        if accepted:
            request.report(object(), True)
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
    service = _service(channel, accepted=False)
    refused = SimpleNamespace(
        runScanExternal=lambda _recalculate, _nonFinal: None
    )

    service.run_scan_from(refused, False, False)

    assert channel.lifecycle == ['start', 'end']


def test_a_raising_dispatch_still_pairs_the_start_it_published():
    channel = _Channel()
    service = _service(channel, accepted=False)

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

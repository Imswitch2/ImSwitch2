"""The single scan API owner (plan A-05): pre-flighted dispatch, exact and
legacy completion handles, cooperative waits, source selection, status."""
import threading
import time
from types import SimpleNamespace

import pytest
from qtpy import QtCore

from imswitch.imcommon.model import (
    CancelToken, OperationCancelled, clearCurrentCancelToken, generateAPI,
    setCurrentCancelToken,
)
from imswitch.imcontrol.controller.WorkflowServices import (
    ScanRequestCompletion, ScanWorkflowService,
)
from imswitch.imcontrol.controller.basecontrollers import ImConWidgetController
from imswitch.imcontrol.controller.controllers.WorkflowFacadeController import (
    WorkflowFacadeController,
)
from imswitch.imcontrol.model.scan_request import (
    ScanRequestRejectedError, ScanRunHandle,
)


class _Signal:
    def __init__(self, log, name):
        self.log = log
        self.name = name
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        self.log.append((self.name,) + args)
        for slot in list(self.slots):
            slot(*args)


class _Channel:
    """Enough of the CommunicationChannel for the workflow service and the
    facade controller: lifecycle signals, source resolution, registries."""

    def __init__(self, sources):
        self.lifecycle = []
        for name in ('sigScanStarting', 'sigScanStarted', 'sigScanDone',
                     'sigScanEnded', 'sigRunScan', 'sigScanRequestRejected'):
            setattr(self, name, _Signal(self.lifecycle, name))
        self.sources = dict(sources)   # key -> source
        self.active = None
        self.preferredKeys = []

    def getActiveScanSource(self):
        return self.active

    def getRecordingScanSourceNames(self):
        return list(self.sources)

    def getRecordingScanSource(self, preferredKey=None):
        self.preferredKeys.append(preferredKey)
        if preferredKey:
            return self.sources[preferredKey]
        if 'Scan' in self.sources:
            return self.sources['Scan']
        if len(self.sources) == 1:
            return next(iter(self.sources.values()))
        raise RuntimeError(f'ambiguous scan sources: {sorted(self.sources)}')

    def controllerRegistry(self):
        return dict(self.sources)


class _Source:
    """A coordinated scan source: pre-flights, reports, binds a completion."""

    def __init__(self, workflowRef, *, refusal='', arm=True):
        self.refusal = refusal
        self.arm = arm
        self.workflowRef = workflowRef
        self.runs = 0
        self.completion = None
        self.token = object()
        self.isRunning = False
        self._scanCoordinator = SimpleNamespace(runForOwner=lambda _o: None)

    def _externalScanStartRefusal(self):
        return self.refusal

    def runScanExternal(self, _recalc, _nonFinal):
        self.runs += 1
        completion = ScanRequestCompletion(self)
        workflow = self.workflowRef()
        if self.arm:
            completion.bind(self.token)
            self.completion = completion
            workflow.report_scan_request_result(self, True, '', self.token, completion)
        else:
            workflow.report_scan_request_result(self, False, 'build failed')


class _LegacySource:
    """Does not pre-flight or report; publishes its own end synchronously."""

    def __init__(self, channel):
        self.channel = channel
        self.runs = 0

    def runScanExternal(self, _recalc, _nonFinal):
        self.runs += 1
        self.channel.sigScanEnded.emit()


@pytest.fixture
def rig(qtbot):
    holder = {}
    source = _Source(lambda: holder['workflow'])
    channel = _Channel({'Scan': source})
    workflow = ScanWorkflowService(channel)
    holder['workflow'] = workflow
    channel.scanWorkflow = workflow
    ctrl = WorkflowFacadeController.__new__(WorkflowFacadeController)
    ImConWidgetController.__init__(
        ctrl, setupInfo=SimpleNamespace(), commChannel=channel,
        master=SimpleNamespace(), widget=None, factory=None, moduleCommChannel=None,
    )
    yield SimpleNamespace(ctrl=ctrl, source=source, channel=channel, workflow=workflow, holder=holder)
    clearCurrentCancelToken()


def test_a_refused_start_raises_and_publishes_no_lifecycle_signal(rig):
    rig.source.refusal = 'This scan controller already has an active iteration.'
    with pytest.raises(ScanRequestRejectedError, match='active iteration'):
        rig.ctrl.runScan()
    assert rig.source.runs == 0
    assert [e[0] for e in rig.channel.lifecycle] == []   # R-07: no start, no end
    # a still-running scan's consumers saw nothing
    assert rig.channel.sigScanEnded.slots == []           # observer removed again


def test_a_rejection_after_preflight_raises_without_a_dangling_observer(rig):
    rig.source.arm = False
    with pytest.raises(ScanRequestRejectedError, match='build failed'):
        rig.ctrl.runScan()
    assert rig.channel.sigScanEnded.slots == []


def test_an_accepted_run_returns_an_exact_handle_that_resolves(rig):
    handle = rig.ctrl.runScan()
    assert isinstance(handle, ScanRunHandle)
    assert handle.exact is True
    assert handle.source == 'Scan'
    assert handle.state == 'pending' and handle.successful is None
    assert rig.channel.sigScanEnded.slots == []           # exact wins, observer gone
    assert rig.ctrl.getScanRequestStatus(handle.requestId)['state'] == 'pending'
    rig.source.completion.resolve(rig.source.token, True, '')
    assert handle.done and handle.successful is True and handle.state == 'succeeded'
    assert rig.ctrl.getScanRequestStatus(handle.requestId) == {
        'requestId': handle.requestId, 'source': 'Scan', 'state': 'succeeded',
        'message': '', 'exact': True,
    }
    assert handle.wait(timeout=0) is True


def test_a_failed_run_resolves_the_handle_as_failed_with_its_message(rig):
    handle = rig.ctrl.runScan()
    rig.source.completion.resolve(rig.source.token, False, 'NI-DAQ write failed')
    assert handle.state == 'failed'
    assert handle.successful is False
    assert handle.message == 'NI-DAQ write failed'


def test_unknown_request_ids_raise():
    ctrl = WorkflowFacadeController.__new__(WorkflowFacadeController)
    with pytest.raises(KeyError):
        ctrl.getScanRequestStatus('nope')


def test_wait_refuses_the_gui_thread_and_is_cooperative_from_a_worker(rig, qtbot):
    handle = rig.ctrl.runScan()
    with pytest.raises(RuntimeError, match='GUI thread'):
        handle.wait()
    outcome = {}
    token = CancelToken()

    def worker():
        setCurrentCancelToken(token)
        try:
            outcome['result'] = handle.wait(timeout=5)
        except BaseException as error:  # noqa: BLE001
            outcome['error'] = error
        finally:
            clearCurrentCancelToken()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.15)
    t0 = time.monotonic()
    token.requestStop()                                   # R-09: Stop while unresolved
    t.join(timeout=2)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 0.5
    assert isinstance(outcome.get('error'), OperationCancelled)
    assert handle.state == 'pending'                      # the scan itself goes on


def test_wait_returns_when_the_run_resolves_and_false_on_timeout(rig, qtbot):
    handle = rig.ctrl.runScan()
    outcome = {}

    def worker():
        outcome['timeout'] = handle.wait(timeout=0.1)
        outcome['result'] = handle.wait(timeout=5)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.2)
    rig.source.completion.resolve(rig.source.token, True, '')
    t.join(timeout=3)
    assert outcome == {'timeout': False, 'result': True}


def test_a_legacy_source_gets_a_handle_resolved_by_the_global_end(qtbot):
    channel = _Channel({})
    legacy = _LegacySource(channel)
    channel.sources['Raster'] = legacy
    workflow = ScanWorkflowService(channel)
    channel.scanWorkflow = workflow
    ctrl = WorkflowFacadeController.__new__(WorkflowFacadeController)
    ImConWidgetController.__init__(
        ctrl, setupInfo=SimpleNamespace(), commChannel=channel,
        master=SimpleNamespace(), widget=None, factory=None, moduleCommChannel=None,
    )
    handle = ctrl.runScan()
    assert legacy.runs == 1
    assert handle.exact is False
    # the end was emitted synchronously inside the call: still caught
    assert handle.state == 'succeeded'
    assert channel.sigScanEnded.slots == []


def test_source_selection_and_ambiguity(rig):
    holder = rig.holder
    second = _Source(lambda: holder['workflow'])
    rig.channel.sources = {'Raster': rig.source, 'LSXYR': second}
    assert rig.ctrl.getScanSourceNames() == ['Raster', 'LSXYR']
    with pytest.raises(RuntimeError, match='ambiguous'):
        rig.ctrl.runScan()
    handle = rig.ctrl.runScan(source='LSXYR')
    assert handle.source == 'LSXYR'
    assert second.runs == 1 and rig.source.runs == 0
    assert rig.channel.preferredKeys[-1] == 'LSXYR'


def test_the_scan_api_is_exported_once_and_a_scanner_exports_no_run_scan(rig):
    from imswitch.imcontrol.controller.basecontrollers import SuperScanController

    class _Scanner(SuperScanController):
        def setParameters(self): pass
        def getParameters(self): pass
        def updatePixels(self): pass
        def runScanAdvanced(self, **_kwargs): pass
        def scanDone(self): pass
        def emitScanSignal(self, signal, *args): signal.emit(*args)

    # The realistic multi-scanner mix: a Scan-widget family controller next to
    # a TriggerScope family one (LSXYR used to re-export runScan itself).
    from imswitch.imcontrol.controller.controllers.TriggerScopeLSXYRController import (
        TriggerScopeLSXYRController,
    )
    scanners = []
    for cls in (_Scanner, TriggerScopeLSXYRController):
        s = cls.__new__(cls)
        ImConWidgetController.__init__(
            s, setupInfo=SimpleNamespace(), commChannel=SimpleNamespace(),
            master=SimpleNamespace(), widget=SimpleNamespace(), factory=None,
            moduleCommChannel=None,
        )
        scanners.append(s)
    api = generateAPI([rig.ctrl] + scanners)           # no NameError (R-13)
    exported = api._asdict()
    assert 'runScan' in exported and 'getScanSourceNames' in exported
    for scanner in scanners:
        assert 'runScan' not in generateAPI([scanner])._asdict()

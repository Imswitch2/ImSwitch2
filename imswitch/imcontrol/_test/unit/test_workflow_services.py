import importlib.util
from pathlib import Path
import threading

import pytest


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_SERVICES_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'WorkflowServices.py'

spec = importlib.util.spec_from_file_location('WorkflowServices', WORKFLOW_SERVICES_PATH)
workflow_services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_services)
BeadRecWorkflowService = workflow_services.BeadRecWorkflowService
ScanWorkflowService = workflow_services.ScanWorkflowService
ScanRequestResult = workflow_services.ScanRequestResult
ScanRequestCompletion = workflow_services.ScanRequestCompletion
ScanDispatch = workflow_services._ScanDispatch


class _Signal:
    def __init__(self) -> None:
        self.emitted = []
        self.slots = []

    def emit(self, *args) -> None:
        self.emitted.append(args)
        for slot in list(self.slots):
            slot(*args)

    def connect(self, slot) -> None:
        if callable(slot):
            self.slots.append(slot)
        else:
            self.emitted.append(('connect', slot))

    def disconnect(self, slot) -> None:
        if slot in self.slots:
            self.slots.remove(slot)


class _CommChannel:
    def __init__(self) -> None:
        self.sigRequestScanParameters = _Signal()
        self.sigRunScan = _Signal()
        self.sigRequestScanFreq = _Signal()
        self.sigSetAxisCenters = _Signal()
        self.sigStartRecordingExternal = _Signal()
        self.sigScanStarting = _Signal()
        self.sigScanEnded = _Signal()
        self.sigAbortScan = _Signal()
        self.sigQueryCenterCoord = _Signal()
        self.sigCenterCoordPipelineFinished = _Signal()
        self.sigUpdateBeadRecCenter = _Signal()
        self.sigShowBeadRecCenterCross = _Signal()
        self.sigAutoAxialToggled = _Signal()
        self.sigNewAxialListBuffer = _Signal()


def test_scan_workflow_service_wraps_legacy_scan_signals():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    workflow.request_scan_parameters()
    workflow.run_scan(True, False)
    workflow.request_scan_frequency()
    workflow.set_axis_centers(['X', 'Y'], [1.0, 2.0])
    workflow.start_external_recording()
    workflow.notify_scan_starting()
    workflow.notify_scan_ended()
    workflow.abort_scan()

    assert comm_channel.sigRequestScanParameters.emitted == [()]
    assert comm_channel.sigRunScan.emitted == [(True, False)]
    assert comm_channel.sigRequestScanFreq.emitted == [()]
    assert comm_channel.sigSetAxisCenters.emitted == [(['X', 'Y'], [1.0, 2.0])]
    assert comm_channel.sigStartRecordingExternal.emitted == [()]
    assert comm_channel.sigScanStarting.emitted == [()]
    assert comm_channel.sigScanEnded.emitted == [()]
    assert comm_channel.sigAbortScan.emitted == [()]


def test_scan_workflow_service_collects_synchronous_acceptance_reports():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    def emit_and_report(*args):
        comm_channel.sigRunScan.emitted.append(args)
        workflow.report_scan_request_result(
            owner,
            True,
            runToken=run_token,
            completion=completion,
        )

    comm_channel.sigRunScan.emit = emit_and_report

    result = workflow.run_scan(True, False)

    assert isinstance(result, ScanRequestResult)
    assert result.handled is True
    assert result.accepted is True
    assert result.reports == ((owner, True, ''),)
    assert result.acceptedTokens == ((owner, run_token),)
    assert result.acceptedCompletions == (
        (owner, run_token, completion),
    )


def test_scan_workflow_service_targets_exact_capable_source():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    run_token = object()

    class Source:
        supportsExactScanRequestCompletion = True

        def __init__(self):
            self.calls = []
            self.completion = ScanRequestCompletion(self)
            self.completion.bind(run_token)

        def runScanExternal(self, recalculate, non_final):
            self.calls.append((recalculate, non_final))
            workflow.report_scan_request_result(
                self,
                True,
                runToken=run_token,
                completion=self.completion,
            )

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    result = workflow.run_scan(True, False)

    assert result.accepted is True
    assert source.calls == [(True, False)]
    assert comm_channel.sigRunScan.emitted == []


def test_prepared_scan_dispatch_serializes_start_with_busy_reservation():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    run_token = object()
    active = False

    class Source:
        supportsExactScanRequestCompletion = True

        def __init__(self):
            self.completion = ScanRequestCompletion(self)
            self.completion.bind(run_token)

        def _externalScanStartRefusal(self):
            return 'scan owner busy' if active else ''

        def runScanExternal(self, *_args):
            nonlocal active
            active = True
            workflow.report_scan_request_result(
                self,
                True,
                runToken=run_token,
                completion=self.completion,
            )

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    first = workflow.run_scan_prepared(True, False)
    second = workflow.run_scan_prepared(True, False)

    assert first.accepted is True
    assert first.startingPublished is True
    assert second.handled is True
    assert second.accepted is False
    assert second.startingPublished is False
    assert second.rejectionMessage == 'scan owner busy'
    assert comm_channel.sigScanStarting.emitted == [()]
    assert comm_channel.sigScanEnded.emitted == []


def test_prepared_start_rejects_retained_run_without_duplicate_lifecycle():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    run_token = object()
    calls = []

    class Coordinator:
        def runForOwner(self, _owner):
            return run_token

    class Source:
        _scanCoordinator = Coordinator()

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            calls.append('run')

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    result = workflow.run_scan_prepared(True, False, True)

    assert result.handled is True
    assert result.accepted is False
    assert result.startingPublished is False
    assert result.endingPublished is False
    assert 'already owns a retained run' in result.rejectionMessage
    assert calls == []
    assert comm_channel.sigScanStarting.emitted == []
    assert comm_channel.sigScanEnded.emitted == []


def test_prepared_continuation_rejects_without_retained_run():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    calls = []

    class Coordinator:
        def runForOwner(self, _owner):
            return None

    class Source:
        _scanCoordinator = Coordinator()

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            calls.append('run')

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    result = workflow.run_scan_prepared(True, True, False)

    assert result.handled is True
    assert result.accepted is False
    assert result.startingPublished is False
    assert result.endingPublished is False
    assert 'requires an existing retained run' in result.rejectionMessage
    assert calls == []
    assert comm_channel.sigScanStarting.emitted == []
    assert comm_channel.sigScanEnded.emitted == []


def test_synchronous_post_start_rejection_is_paired_on_ui_thread():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    events = []

    class Coordinator:
        def runForOwner(self, _owner):
            return None

    class Source:
        _scanCoordinator = Coordinator()

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            events.append(('run', threading.current_thread()))
            workflow.report_scan_request_result(
                self, False, 'arm refused after pre-arm'
            )

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source
    comm_channel.sigScanStarting.emit = lambda: events.append(
        ('start', threading.current_thread())
    )
    originalEndEmit = comm_channel.sigScanEnded.emit

    def emitEnd():
        events.append(('end', threading.current_thread()))
        originalEndEmit()

    comm_channel.sigScanEnded.emit = emitEnd

    result = workflow.run_scan_prepared(True, False, True)

    assert result.handled is True
    assert result.accepted is False
    assert result.startingPublished is True
    assert result.endingPublished is True
    assert events == [
        ('start', threading.main_thread()),
        ('run', threading.main_thread()),
        ('end', threading.main_thread()),
    ]


def test_prepared_prearm_exception_pairs_end_in_same_ui_transaction(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    events = []
    calls = []

    class Coordinator:
        def runForOwner(self, _owner):
            return None

    class Source:
        _scanCoordinator = Coordinator()

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            calls.append('run')

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    def emitStart():
        events.append(('start', threading.current_thread()))
        raise RuntimeError('pre-arm listener failed')

    comm_channel.sigScanStarting.emit = emitStart
    originalEndEmit = comm_channel.sigScanEnded.emit

    def emitEnd():
        events.append(('end', threading.current_thread()))
        originalEndEmit()

    comm_channel.sigScanEnded.emit = emitEnd
    errors = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: workflow.run_scan_prepared(True, False, True),
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert len(errors) == 1
    assert str(errors[0]) == 'pre-arm listener failed'
    request = errors[0].scanRequestResult
    assert request.startingPublished is True
    assert request.endingPublished is True
    assert request.handled is False
    assert calls == []
    assert events == [
        ('start', threading.main_thread()),
        ('end', threading.main_thread()),
    ]
    assert comm_channel.sigScanEnded.slots == []


def test_two_worker_prepared_scans_publish_only_the_accepted_start(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    run_token = object()
    active_source = None
    results = []
    errors = []

    class Source:
        supportsExactScanRequestCompletion = True

        def __init__(self):
            self.completion = ScanRequestCompletion(self)
            self.completion.bind(run_token)

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            nonlocal active_source
            active_source = self
            workflow.report_scan_request_result(
                self,
                True,
                runToken=run_token,
                completion=self.completion,
            )

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source
    comm_channel.getActiveScanSource = lambda: active_source
    barrier = threading.Barrier(3)

    def runPrepared():
        barrier.wait()
        _capture_result(
            results,
            errors,
            lambda: workflow.run_scan_prepared(True, False, True),
        )

    threads = [threading.Thread(target=runPrepared) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    qtbot.waitUntil(
        lambda: all(not thread.is_alive() for thread in threads),
        timeout=2000,
    )
    for thread in threads:
        thread.join(timeout=0.1)

    assert errors == []
    assert len(results) == 2
    assert sum(result.accepted for result in results) == 1
    rejected = next(result for result in results if not result.accepted)
    assert rejected.handled is True
    assert rejected.startingPublished is False
    assert rejected.rejectionMessage == (
        'Another scan source is already active.'
    )
    assert comm_channel.sigScanStarting.emitted == [()]
    assert comm_channel.sigScanEnded.emitted == []


def test_prepared_continuation_emits_no_new_global_start():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    calls = []

    runToken = object()

    class Coordinator:
        def runForOwner(self, _owner):
            return runToken

    class Source:
        _scanCoordinator = Coordinator()

        def _externalScanStartRefusal(self):
            return ''

        def runScanExternal(self, *_args):
            calls.append('run')

    comm_channel.getRecordingScanSource = lambda: Source()

    result = workflow.run_scan_prepared(False, True, False)

    assert result.startingPublished is False
    assert calls == ['run']
    assert comm_channel.sigScanStarting.emitted == []


def test_scan_workflow_service_targets_unique_legacy_source_without_broadcast():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    class Source:
        def __init__(self):
            self.calls = []

        def runScanExternal(self, recalculate, non_final):
            self.calls.append((recalculate, non_final))

    source = Source()
    comm_channel.getRecordingScanSource = lambda: source

    result = workflow.run_scan(True, False)

    assert result.handled is False
    assert source.calls == [(True, False)]
    assert comm_channel.sigRunScan.emitted == []
    assert workflow.exact_wait_supported() is False


def test_scan_workflow_service_propagates_ambiguous_source_without_broadcast():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    def fail_resolution():
        raise RuntimeError('multiple capable controllers are registered')

    comm_channel.getRecordingScanSource = fail_resolution

    with pytest.raises(RuntimeError, match='multiple capable controllers'):
        workflow.run_scan(True, False)

    assert comm_channel.sigRunScan.emitted == []


def test_scan_request_completion_rejects_a_stale_run_token():
    owner = object()
    active_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(active_token)

    assert completion.resolve(object(), True) is False
    assert completion.wait(timeout=0) is False

    assert completion.resolve(active_token, True) is True
    assert completion.wait(timeout=0) is True
    assert completion.successful is True


def test_scan_request_completion_callbacks_are_exact_and_once_only():
    owner = object()
    token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(token)
    callbacks = []

    completion.add_done_callback(
        lambda resolved: callbacks.append(("early", resolved))
    )
    assert completion.resolve(object(), True) is False
    assert callbacks == []

    assert completion.resolve(token, True) is True
    assert callbacks == [("early", completion)]
    assert completion.resolve(token, False, "late") is False
    assert callbacks == [("early", completion)]

    completion.add_done_callback(
        lambda resolved: callbacks.append(("late", resolved))
    )
    assert callbacks == [
        ("early", completion),
        ("late", completion),
    ]


def test_cancelled_queued_scan_dispatch_never_runs_late():
    calls = []
    dispatch = ScanDispatch(lambda: calls.append(True))

    assert dispatch.cancelIfQueued() is True
    assert dispatch.begin() is False

    workflow = ScanWorkflowService(_CommChannel())
    workflow._execute_scan_dispatch(dispatch)

    assert dispatch.finished.wait(timeout=0) is True
    assert calls == []


def test_scan_workflow_service_distinguishes_unhandled_legacy_receiver():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    result = workflow.run_scan(True, False)

    assert result.handled is False
    assert result.accepted is False


def test_scan_workflow_service_targets_one_source_without_broadcast():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    class Source:
        def __init__(self):
            self.run_calls = []
            self.abort_calls = 0

        def runScanExternal(self, recalculate, non_final):
            self.run_calls.append((recalculate, non_final))

        def abortScan(self):
            self.abort_calls += 1

    source = Source()
    result = workflow.run_scan_from(source, True, False)
    workflow.abort_scan_from(source)

    assert result.handled is False
    assert source.run_calls == [(True, False)]
    assert source.abort_calls == 1
    assert comm_channel.sigRunScan.emitted == []
    assert comm_channel.sigAbortScan.emitted == []


def test_targeted_scan_dispatch_marshals_worker_call_to_ui_thread(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    calls = []
    source = type(
        'Source',
        (),
        {
            'runScanExternal': (
                lambda *_args: calls.append(threading.current_thread())
            )
        },
    )()
    errors = []

    thread = threading.Thread(
        target=lambda: _capture_error(
            errors, lambda: workflow.run_scan_from(source, True, False)
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert errors == []
    assert calls == [threading.main_thread()]


def test_targeted_abort_marshals_worker_call_to_ui_thread(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    run_token = object()
    calls = []

    class Coordinator:
        def runForOwner(self, _owner):
            return run_token

    source = type(
        'Source',
        (),
        {
            '_scanCoordinator': Coordinator(),
            'abortScan': (
                lambda *_args: calls.append(threading.current_thread())
            ),
        },
    )()
    errors = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: workflow.abort_scan_from(source, run_token),
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert errors == []
    assert calls == [threading.main_thread()]


def test_stale_targeted_abort_cannot_stop_new_owner_generation(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    stale_token = object()
    current_token = object()
    calls = []

    class Coordinator:
        def runForOwner(self, _owner):
            return current_token

    source = type(
        'Source',
        (),
        {
            '_scanCoordinator': Coordinator(),
            'abortScan': lambda *_args: calls.append(True),
        },
    )()
    errors = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: workflow.abort_scan_from(source, stale_token),
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert errors == []
    assert calls == []


def test_targeted_abort_without_verifiable_identity_fails_closed(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    calls = []
    source = type(
        'OpaqueSource',
        (),
        {'abortScan': lambda *_args: calls.append(True)},
    )()
    errors = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: workflow.abort_scan_from(source, object()),
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert errors == []
    assert calls == []


def test_worker_broadcast_collects_same_thread_acceptance_on_ui(qtbot):
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)
    dispatch_threads = []
    results = []
    errors = []

    def emit_and_report(*args):
        comm_channel.sigRunScan.emitted.append(args)
        dispatch_threads.append(threading.current_thread())
        workflow.report_scan_request_result(
            owner,
            True,
            runToken=run_token,
            completion=completion,
        )

    comm_channel.sigRunScan.emit = emit_and_report
    thread = threading.Thread(
        target=lambda: _capture_result(
            results, errors, lambda: workflow.run_scan(True, False)
        )
    )
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=2000)
    thread.join(timeout=0.1)

    assert errors == []
    assert dispatch_threads == [threading.main_thread()]
    assert results[0].acceptedCompletions == (
        (owner, run_token, completion),
    )


def test_blocking_facade_rejects_ui_thread_before_dispatch():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    with pytest.raises(RuntimeError, match='cannot run on the UI thread'):
        ScanWorkflowFacade(
            workflow, comm_channel.sigScanEnded
        ).run_once()

    assert comm_channel.sigRunScan.emitted == []


def test_blocking_legacy_service_fails_before_worker_dispatch():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)
    facade = ScanWorkflowFacade(workflow, comm_channel.sigScanEnded)
    errors = []

    thread = threading.Thread(
        target=lambda: _capture_error(
            errors, lambda: facade.run_once(timeout_s=0.1)
        )
    )
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert 'exact-capable scan source' in str(errors[0])
    assert comm_channel.sigRunScan.emitted == []


def _capture_error(errors, action):
    try:
        action()
    except Exception as error:
        errors.append(error)


def _capture_result(results, errors, action):
    try:
        results.append(action())
    except Exception as error:
        errors.append(error)


def test_bead_rec_workflow_service_wraps_legacy_signals():
    comm_channel = _CommChannel()
    workflow = BeadRecWorkflowService(comm_channel)
    slot = object()

    workflow.query_center_coord('Maxima')
    workflow.finish_center_coord_pipeline((1, 2))
    workflow.update_bead_rec_center(3, 4)
    workflow.show_bead_rec_center_cross(True)
    workflow.set_auto_axial(False)
    workflow.set_axial_list_buffer(['XZ'])
    workflow.on_query_center_coord(slot)
    workflow.on_center_coord_pipeline_finished(slot)
    workflow.on_update_bead_rec_center(slot)
    workflow.on_show_bead_rec_center_cross(slot)
    workflow.on_auto_axial_toggled(slot)
    workflow.on_new_axial_list_buffer(slot)

    assert comm_channel.sigQueryCenterCoord.emitted == [('Maxima',), ('connect', slot)]
    assert comm_channel.sigCenterCoordPipelineFinished.emitted == [((1, 2),), ('connect', slot)]
    assert comm_channel.sigUpdateBeadRecCenter.emitted == [(3, 4), ('connect', slot)]
    assert comm_channel.sigShowBeadRecCenterCross.emitted == [(True,), ('connect', slot)]
    assert comm_channel.sigAutoAxialToggled.emitted == [(False,), ('connect', slot)]
    assert comm_channel.sigNewAxialListBuffer.emitted == [(['XZ'],), ('connect', slot)]

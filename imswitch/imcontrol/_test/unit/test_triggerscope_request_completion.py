"""TriggerScope scan requests always report acceptance/refusal reasons.
Exact-completion requests additionally receive a completion object resolved by
the scan terminal; Recording-style requests keep their legacy completion path."""
from types import SimpleNamespace

from imswitch.imcontrol.controller.controllers._triggerscope_scan_lifecycle import (
    TriggerScopeScanLifecycleMixin,
)


class _Workflow:
    def __init__(self, exact):
        self.exact = exact
        self.reports = []

    def active_request_wants_exact_completion(self):
        return self.exact

    def report_scan_request_result(self, owner, accepted, message='', runToken=None, completion=None):
        self.reports.append((owner, accepted, message, runToken, completion))


class _Shell(TriggerScopeScanLifecycleMixin):
    def __init__(self, workflow, *, bind):
        self._commChannel = SimpleNamespace(scanWorkflow=workflow, sigScanRequestRejected=None)
        self._widget = SimpleNamespace(setRepeatEnabled=lambda *_a: None)
        self._logger = SimpleNamespace(error=lambda *a, **k: None, debug=lambda *a, **k: None,
                                       warning=lambda *a, **k: None)
        self._scanCoordinator = SimpleNamespace(
            runForOwner=lambda _owner: self.token if self.bind else None
        )
        self._externalTriggerScopeCompletion = None
        self._triggerScopeBoundCompletions = []
        self._triggerScopeRunOutcome = None
        self._lastScanStartRejection = None
        self.bind = bind
        self.token = object()

    def runScanAdvanced(self, **_kwargs):
        completion = self._externalTriggerScopeCompletion
        if self.bind and completion is not None:
            completion.bind(self.token)
            self._triggerScopeBoundCompletions.append(completion)
        elif not self.bind:
            self._lastScanStartRejection = 'Ignoring duplicate TriggerScope scan start'


def test_recording_style_request_reports_acceptance_without_exact_completion():
    workflow = _Workflow(exact=False)
    shell = _Shell(workflow, bind=True)
    shell._runTriggerScopeScanExternal(True, False)

    (owner, accepted, message, runToken, completion), = workflow.reports
    assert owner is shell
    assert accepted is True
    assert message == ''
    assert runToken is shell.token
    assert completion is None
    assert shell._triggerScopeBoundCompletions == []


def test_an_exact_request_is_accepted_bound_and_resolved_from_the_terminal():
    workflow = _Workflow(exact=True)
    shell = _Shell(workflow, bind=True)
    shell._runTriggerScopeScanExternal(True, False)
    (owner, accepted, message, runToken, completion), = workflow.reports
    assert owner is shell and accepted is True and runToken is shell.token
    assert completion.runToken is shell.token and not completion.wait(0)
    shell._resolveTriggerScopeCompletions(shell.token, True, '')
    assert completion.wait(0) and completion.successful is True
    assert shell._triggerScopeBoundCompletions == []


def test_an_exact_request_that_was_refused_reports_the_reason():
    workflow = _Workflow(exact=True)
    shell = _Shell(workflow, bind=False)
    shell._runTriggerScopeScanExternal(True, False)
    (owner, accepted, message, runToken, completion), = workflow.reports
    assert accepted is False and 'duplicate' in message
    assert runToken is None and completion is None

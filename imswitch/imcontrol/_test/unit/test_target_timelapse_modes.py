"""Tests for TargetTimelapseWorkflow phase T3 setup mode integration.

Focused unit tests covering:
- Preflight before stage motion
- Event role applied before each acquisition callback
- Resume role applied after each acquisition (optional)
- Idle role applied at completion or failure
- Mode apply failure aborts before unsafe acquisition
- No mode calls when mode_adapter/mode_config omitted (backward compatibility)
"""

import pytest
from dataclasses import dataclass, field
from typing import List, Optional

from imswitch.imcontrol.model.workflows import (
    ModeConfig,
    ModeTransition,
    TargetList,
    TargetTimelapseParams,
    TargetTimelapseWorkflow,
    build_mock_facade,
)


@dataclass
class FakeApplyResult:
    """Fake ApplyResult for testing."""
    applied: bool
    ok: bool
    modeName: Optional[str]
    warnings: List[str] = field(default_factory=list)
    failedComponents: List[str] = field(default_factory=list)


@dataclass
class FakePreflightResult:
    """Fake PreflightResult for testing."""
    ok: bool
    hazards: List[dict] = field(default_factory=list)
    missingModes: List[str] = field(default_factory=list)
    failedModes: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)


class FakeModeService:
    """Fake SmartMicroscopyModeService for testing."""

    def __init__(self):
        self.preflight_calls = []
        self.apply_calls = []
        self.events = []
        self.preflight_ok = True
        self.preflight_messages = []
        self.apply_ok = True
        self.apply_mode_name = "FakeMode"

    def preflight(self, workflow_name, roles=None):
        self.events.append(("preflight", workflow_name, list(roles or [])))
        self.preflight_calls.append((workflow_name, roles))
        return FakePreflightResult(
            ok=self.preflight_ok,
            messages=list(self.preflight_messages),
        )

    def applyRole(self, workflow_name, role):
        self.events.append(("apply", workflow_name, role))
        self.apply_calls.append((workflow_name, role))
        return FakeApplyResult(
            applied=True,
            ok=self.apply_ok,
            modeName=self.apply_mode_name if self.apply_ok else None,
        )


class FakeModeAdapter:
    """Fake SmartModeWorkflowAdapter for testing that records all calls."""

    def __init__(self, fake_service):
        self._service = fake_service
        self._transitions = []

    def preflight(self, workflow_name, roles=None):
        return self._service.preflight(workflow_name, roles)

    def applyRole(self, workflow_name, role, required=True):
        result = self._service.applyRole(workflow_name, role)
        transition = ModeTransition(
            workflow_name=workflow_name,
            role=role,
            timestamp_s=0.0,
            applied=result.applied,
            ok=result.ok,
            mode_name=result.modeName,
            warnings=list(result.warnings),
            failed_components=list(result.failedComponents),
        )
        self._transitions.append(transition)
        return result

    def applyIdleFallback(self, workflow_name, role="idle"):
        return self.applyRole(workflow_name, role, required=False)

    def getTransitions(self):
        return list(self._transitions)

    def clearTransitions(self):
        self._transitions.clear()


@pytest.mark.nohardware
def test_mode_preflight_runs_before_stage_motion():
    """Preflight is called before any stage movement when mode integration is enabled."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", preflight_roles=["event"])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)
    original_move_to = facade.stage_con.move_to

    def move_to_with_event(x, y):
        fake_service.events.append(("move_to", x, y))
        original_move_to(x, y)

    facade.stage_con.move_to = move_to_with_event

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        workflow_name="TestWorkflow",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert len(fake_service.preflight_calls) == 1
    assert fake_service.preflight_calls[0] == ("TestWorkflow", ["event"])
    assert result.preflight_passed is True
    assert fake_service.events[0][0] == "preflight"
    assert fake_service.events[1][0] == "apply"
    assert fake_service.events[2][0] == "move_to"

    stage_moves = [c for c in facade.calls if c[0] == "stage_con.move_to"]
    assert len(stage_moves) == 1


@pytest.mark.nohardware
def test_preflight_failure_aborts_before_stage_motion():
    """When preflight fails, workflow aborts before moving stage or acquiring."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event")

    fake_service = FakeModeService()
    fake_service.preflight_ok = False
    fake_service.preflight_messages = ["Hazard detected: high laser power"]
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert result.preflight_passed is False
    assert "Hazard detected" in result.preflight_messages[0]
    assert result.mode_failure is True
    assert len(result.acquisitions) == 0

    stage_moves = [c for c in facade.calls if c[0] == "stage_con.move_to"]
    assert len(stage_moves) == 0


@pytest.mark.nohardware
def test_event_role_applied_before_each_acquisition():
    """Event role is applied before each target acquisition callback."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    targets.add(stage_xy=(150.0, 250.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=2)
    mode_config = ModeConfig(event_role="event", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    original_move_to = facade.stage_con.move_to

    def move_to_with_event(x, y):
        fake_service.events.append(("move_to", x, y))
        original_move_to(x, y)

    facade.stage_con.move_to = move_to_with_event

    def callback(target, tp):
        fake_service.events.append(("callback", target.id, tp))
        return "ok"

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, callback,
        workflow_name="TestWorkflow",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    event_applies = [c for c in fake_service.apply_calls if c[1] == "event"]
    assert len(event_applies) == 4

    assert event_applies[0] == ("TestWorkflow", "event")
    assert event_applies[1] == ("TestWorkflow", "event")
    assert event_applies[2] == ("TestWorkflow", "event")
    assert event_applies[3] == ("TestWorkflow", "event")

    assert len(result.acquisitions) == 4
    for i in range(0, 12, 3):
        assert fake_service.events[i][0] == "apply"
        assert fake_service.events[i + 1][0] == "move_to"
        assert fake_service.events[i + 2][0] == "callback"
    for acq in result.acquisitions:
        event_transitions = [t for t in acq.mode_transitions if t.role == "event"]
        assert len(event_transitions) == 1
        assert event_transitions[0].ok is True


@pytest.mark.nohardware
def test_resume_role_applied_after_acquisition_when_configured():
    """Resume role is applied after each acquisition when configured."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=2)
    mode_config = ModeConfig(event_role="event", resume_role="resume", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    event_applies = [c for c in fake_service.apply_calls if c[1] == "event"]
    resume_applies = [c for c in fake_service.apply_calls if c[1] == "resume"]

    assert len(event_applies) == 2
    assert len(resume_applies) == 2

    assert len(result.acquisitions) == 2
    for acq in result.acquisitions:
        assert len(acq.mode_transitions) == 2
        assert acq.mode_transitions[0].role == "event"
        assert acq.mode_transitions[1].role == "resume"


@pytest.mark.nohardware
def test_no_resume_role_when_not_configured():
    """Resume role is not applied when mode_config.resume_role is None."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", resume_role=None, preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    resume_applies = [c for c in fake_service.apply_calls if c[1] == "resume"]
    assert len(resume_applies) == 0

    assert len(result.acquisitions) == 1
    assert len(result.acquisitions[0].mode_transitions) == 1
    assert result.acquisitions[0].mode_transitions[0].role == "event"


@pytest.mark.nohardware
def test_idle_role_applied_at_completion():
    """Idle role is applied at workflow completion when configured."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", idle_role="safe_idle", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        workflow_name="TestWorkflow",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    idle_applies = [c for c in fake_service.apply_calls if c[1] == "safe_idle"]
    assert len(idle_applies) == 1
    assert idle_applies[0] == ("TestWorkflow", "safe_idle")

    assert result.idle_transition is not None
    assert result.idle_transition.role == "safe_idle"


@pytest.mark.nohardware
def test_idle_role_applied_after_preflight_failure():
    """Idle role is applied when workflow aborts due to preflight failure."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", idle_role="idle")

    fake_service = FakeModeService()
    fake_service.preflight_ok = False
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    idle_applies = [c for c in fake_service.apply_calls if c[1] == "idle"]
    assert len(idle_applies) == 1

    assert result.mode_failure is True
    assert result.idle_transition is not None


@pytest.mark.nohardware
def test_event_role_failure_aborts_acquisition():
    """When event role fails, workflow aborts before stage motion regardless of policy."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    targets.add(stage_xy=(150.0, 250.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, failure_policy="continue")
    mode_config = ModeConfig(event_role="event", idle_role="idle", preflight_roles=[])

    fake_service = FakeModeService()
    fake_service.apply_ok = False
    mode_adapter = FakeModeAdapter(fake_service)

    callback_called = []
    workflow = TargetTimelapseWorkflow(
        facade, targets, params,
        lambda t, tp: callback_called.append(True) or "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert len(callback_called) == 0
    stage_moves = [c for c in facade.calls if c[0] == "stage_con.move_to"]
    assert len(stage_moves) == 0

    assert len(result.acquisitions) == 1
    assert result.acquisitions[0].success is False
    assert result.acquisitions[0].mode_failure is True
    assert "Event role 'event' failed to apply" in result.acquisitions[0].error
    assert result.mode_failure is True
    assert result.aborted is True

    idle_applies = [c for c in fake_service.apply_calls if c[1] == "idle"]
    assert len(idle_applies) == 1


@pytest.mark.nohardware
def test_no_mode_calls_when_mode_adapter_omitted():
    """When mode_adapter and mode_config are None, no mode operations occur (backward compatibility)."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    targets.add(stage_xy=(150.0, 250.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=2)

    callback_called = []
    workflow = TargetTimelapseWorkflow(
        facade, targets, params,
        lambda t, tp: callback_called.append((t.id, tp)) or "ok",
    )
    result = workflow.run()

    assert len(callback_called) == 4
    assert result.preflight_passed is None
    assert len(result.preflight_messages) == 0
    assert result.idle_transition is None
    assert len(result.acquisitions) == 4
    for acq in result.acquisitions:
        assert len(acq.mode_transitions) == 0


@pytest.mark.nohardware
def test_mode_adapter_and_config_must_both_be_supplied():
    """Supplying only mode_adapter or only mode_config raises ValueError."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    params = TargetTimelapseParams(n_timepoints=1)

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)
    mode_config = ModeConfig(event_role="event")

    with pytest.raises(ValueError, match="mode_adapter and mode_config must both be supplied"):
        TargetTimelapseWorkflow(
            facade, targets, params, lambda t, tp: "ok",
            mode_adapter=mode_adapter,
            mode_config=None,
        )

    with pytest.raises(ValueError, match="mode_adapter and mode_config must both be supplied"):
        TargetTimelapseWorkflow(
            facade, targets, params, lambda t, tp: "ok",
            mode_adapter=None,
            mode_config=mode_config,
        )


@pytest.mark.nohardware
def test_mode_transitions_recorded_per_acquisition():
    """Mode transitions (event and resume) are recorded in each acquisition result."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", resume_role="scouting", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        workflow_name="TestWorkflow",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert len(result.acquisitions) == 1
    acq = result.acquisitions[0]

    assert len(acq.mode_transitions) == 2
    assert acq.mode_transitions[0].workflow_name == "TestWorkflow"
    assert acq.mode_transitions[0].role == "event"
    assert acq.mode_transitions[0].ok is True
    assert acq.mode_transitions[1].role == "scouting"


@pytest.mark.nohardware
def test_idle_not_applied_when_idle_role_is_none():
    """Idle role is not applied when mode_config.idle_role is None."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", idle_role=None, preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    idle_applies = [c for c in fake_service.apply_calls if c[1] == "idle"]
    assert len(idle_applies) == 0
    assert result.idle_transition is None


@pytest.mark.nohardware
def test_empty_targets_do_not_apply_modes():
    """When no targets are enabled, no mode transitions are applied."""
    facade = build_mock_facade()
    targets = TargetList()

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", idle_role="idle", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert len(result.acquisitions) == 0
    assert fake_service.preflight_calls == []
    assert fake_service.apply_calls == []
    assert result.idle_transition is None


@pytest.mark.nohardware
def test_preflight_roles_defaults_to_event_role():
    """When preflight_roles is None, it defaults to [event_role]."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="myevent")

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert len(fake_service.preflight_calls) == 1
    assert fake_service.preflight_calls[0][1] == ["myevent"]


@pytest.mark.nohardware
def test_empty_preflight_roles_skips_preflight():
    """An explicit empty preflight role list skips preflight calls."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)
    mode_config = ModeConfig(event_role="event", preflight_roles=[])

    fake_service = FakeModeService()
    mode_adapter = FakeModeAdapter(fake_service)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: "ok",
        mode_adapter=mode_adapter,
        mode_config=mode_config,
    )
    result = workflow.run()

    assert fake_service.preflight_calls == []
    assert result.preflight_passed is None

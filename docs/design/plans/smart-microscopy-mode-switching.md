# Smart Microscopy Mode Switching Plan

## Problem

`SetupStatusController` currently owns Snouty-specific mode switching:

- it listens to `CommunicationChannel.sigSetConfig`;
- it maps hard-coded names such as `Widefield imaging` and `Light sheet imaging`;
- it moves flip mirrors directly;
- it updates display labels and visible image layers;
- it contains a KDC-backed rotation-stage control path that is not actually part
  of setup status.

This is too specific for one microscope. The new setup modes are the better
source of truth for "put the microscope in this hardware state", but event
triggered workflows need a small programmatic transition layer on top of setup
modes.

The desired end state is:

- `SetupStatus` can be removed from the Snouty setup.
- KDC hardware is managed by a normal positioner manager.
- smart microscopy controllers can say "apply scouting mode", "apply event
  mode", and "apply recovery mode" without knowing which mirrors, lasers,
  stand modes, SLMs, cameras, or detector settings are involved.
- mode switching is reusable across EtSnouty, EtSTED, EtMonalisa, and future
  event-triggered modalities.

## Implementation Status

Updated 2026-06-21.

- **Phase 1 implemented:** role mapping lives in `SetupInfo.smartMicroscopyModes`;
  `SmartMicroscopyModeService` resolves and applies roles through
  `SetupModeController`; `ImConMainController` injects the service into event
  controllers that expose `setSmartModeService(...)`.
- **Phase 2 implemented:** `SetupModeController.applySetupMode(...)` returns
  `ApplyOutcome`; `loadSetupMode(...)` remains the legacy warning-list wrapper;
  service preflight uses `getModeHazards(...)`; hardware component warnings and
  failures become `ApplyResult.ok == False`.
- **Phase 3 implemented behind rollout flag:** EtSnouty uses smart-mode roles
  when `smartMicroscopyModeSwitchingEnabled["EtSnouty"]` is true and keeps the
  legacy `sigSetConfig` path when the flag is absent or false.
- **Phase 4 in-repo fixture implemented:** a no-hardware Snouty smart-mode
  setup boots in CI without `SetupStatus`; KDC is represented as a positioner in
  that fixture. External lab setup migration still requires hardware-side
  verification.
- **Phase 5 implemented:** EtSnouty and the shared event-triggered base use the
  same `SmartModeRoleMixin`; EtSTED and EtMonalisa can opt into role mappings.
- **Phase 5b implemented:** Leica stand FLUO/CS mode is represented as setup
  mode state for EtMonalisa smart-mode operation.
- **Phase 6 implemented:** `SetupModesWidget` exposes role-assignment UI,
  `SetupModesController` saves mappings through `SmartMicroscopyModeService`,
  and the event-triggered widget can display configured workflow roles.
- **Sanity-check fixes included:** failed setup-mode applies clear the
  "last clean mode" marker before hardware recovery, and failed EtSnouty event
  role application stops acquisition instead of resuming endless scouting.
- **Software verification:** focused smart-mode unit suites pass with
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.
- **Remaining work:** live Snouty hardware validation, external setup-file
  migration, and final operator workflow polish.

## Current State Review

### EtSnouty

`EtSnoutyController` is still a standalone legacy event-triggered controller.
It calls `setConfig(widefield=True)` when the experiment starts and when it
resumes fast widefield detection after a slow scan. It calls
`setConfig(widefield=False)` when an event is detected and a slow scan is about
to run.

With `smartMicroscopyModeSwitchingEnabled["EtSnouty"]` disabled, those calls
still emit `sigSetConfig("Widefield imaging")` and
`sigSetConfig("Light sheet imaging")`; `SetupStatusController` receives the
signal and moves the Snouty flip mirrors. With the flag enabled, those same
transition points apply smart microscopy roles through
`SmartMicroscopyModeService`. This happens on transitions, not on every frame:

- on arm/start: apply widefield/scouting;
- on detected event: apply light-sheet/event mode;
- after scan end with endless mode enabled: apply widefield/scouting again;
- on stop/failure: disable the selected fast laser and apply `idle` if that role
  is configured.

### EtSTED and EtMonalisa

`EtSTEDController` and `EtMonalisaController` already use
`EventTriggeredControllerBase`. The base has the right lifecycle hooks:

- `_pre_arm_hook`;
- `_on_pause_modality_hook`;
- `_on_resume_modality_hook`;
- `_post_stop_hook`.

EtMonalisa uses those hooks to switch a Leica stand between fluorescence and
confocal modes. That is the same class of problem as Snouty's widefield vs
light-sheet mirror switching, but it is currently encoded as direct
stand-manager commands rather than setup-mode application.

### Setup Modes

Setup modes already snapshot and apply component state through
`SetupModeController` and the unified state persistence registry. They already
cover hardware-changing components such as `Laser`, `FlipMirror`, `LeicaStand`,
`SLM`/`SLMs`, and `Scan`. This is the right mechanism for declaring microscope
states.

Two gaps matter for automated event-time switching:

- `SetupModesController` owns the UI confirmation flow for hazards, but
  event-time mode switches cannot block on a dialog.
- setup modes are selected by user-facing mode name, not by runtime role such as
  `scouting` or `event`.

## Proposed Model

Introduce a small "smart microscopy mode transition" layer that maps runtime
roles to existing setup mode names and applies them through the setup-mode
backend.

Runtime roles:

- `scouting`: fast modality used for event detection, e.g. widefield.
- `event`: hardware state required before the triggered slow/high-resolution
  acquisition, e.g. light sheet, confocal, STED.
- `resume`: optional mode applied after a slow scan before returning to
  detection. Defaults to `scouting`.
- `idle`: optional safe mode applied on stop, failure, or close.
- `validation`: optional mode for `TestValidate`/`TestVisualize`. Defaults to
  `scouting` or no transition depending on workflow configuration.

The event controller never hard-codes mirror names, stand commands, laser
states, or detector visibility. It asks the transition layer to apply a role.

Example conceptual mapping:

```json
"smartMicroscopyModes": {
  "EtSnouty": {
    "scouting": "Snouty widefield scouting",
    "event": "Snouty light-sheet event scan",
    "resume": "Snouty widefield scouting",
    "idle": "Snouty safe idle"
  },
  "EtMonalisa": {
    "scouting": "MoNaLISA fluorescence scouting",
    "event": "MoNaLISA confocal event scan",
    "resume": "MoNaLISA fluorescence scouting",
    "idle": "MoNaLISA safe idle"
  }
},
"smartMicroscopyModeSwitchingEnabled": {
  "EtSnouty": true
}
```

The exact storage can be either:

- a setup-file section such as `smartMicroscopyModes`, which is explicit and
  versionable with the setup; or
- metadata on setup mode files, if setup modes grow first-class role tags.

The setup-file section is the safer first step because it does not overload
mode names or require changing the setup-mode file schema immediately.

## Design Principles

These principles drive the architecture below. They exist because the current
`sigSetConfig` path is a fire-and-forget signal: the emitter cannot know whether
the hardware actually reached the requested state, which is exactly what makes
safe automated switching impossible today.

1. **The service is the reuse boundary, not the base class.**
   `EventTriggeredControllerBase` is just a convenient caller. EtSnouty (not yet
   on the base) and the base-class controllers must reach the *same* service by
   the same method calls. The shared abstraction is the service object, so the
   base-class port can happen independently of mode switching.

2. **Role application is a synchronous call that returns warnings, never a
   signal.** Event controllers need the result inline to make safety decisions
   (block arming, recover to `idle`). This is the core upgrade over
   `sigSetConfig`. The service exposes `applyRole(...) -> list[warning]` /
   `preflight(...) -> Hazards`, not a Qt signal.

3. **The backend owns primitives; the consumer owns policy.**
   `SetupModeController` already provides the two primitives the service needs:
   `loadSetupMode` (apply) and `getModeHazards` (detect). It deliberately knows
   nothing about hazard *policy*. There are two independent consumers of those
   primitives — the interactive `SetupModesController` (policy: show a dialog)
   and the new service (policy: non-interactive, preflighted). The service must
   call the backend directly and must **not** route through
   `SetupModesController`, whose apply path is gated on a modal dialog.

4. **There is one microscope, so there is one active hardware mode.** Redundant
   reapplication is suppressed by comparing the *resolved mode name* against a
   single shared "last applied mode" owned by the backend
   (`SetupModeController`), updated by both consumers. De-duping per workflow
   would be wrong because the hardware state is global.

## Architecture

### 1. Smart Mode Transition Service

Add a service object, for example `SmartMicroscopyModeService`, created by
`ImConMainController` immediately after `self.setupModeController` is built
([`ImConMainController.py:77`](../../../imswitch/imcontrol/controller/ImConMainController.py)).
It takes the `SetupModeController` and the parsed role config as constructor
arguments — a backend object, peer to `SetupModeController`, with no widget.

Responsibilities:

- resolve `(workflowName, role)` to a setup mode name from `setupInfo`;
- perform startup validation that all configured role modes exist;
- apply a role by calling the backend **directly** and returning an
  `ApplyResult` synchronously to the caller (`applied`, `ok`, `warnings`,
  `failedComponents`) — richer than a bare warning list so the caller can branch
  on success/failure (see §2);
- suppress redundant reapplication by comparing the resolved mode name against
  the backend's shared "last applied mode" (Design Principle 4);
- run a non-interactive hazard preflight via `SetupModeController.getModeHazards`
  and apply the configured policy (see §2);
- return — never emit — warnings so event controllers can log them and decide.

Injection: event controllers receive the service through a setter
(`setSmartModeService(...)`), wired from `ImConMainController` the same way
`SetupModes` receives the backend at
[`ImConMainController.py:78`](../../../imswitch/imcontrol/controller/ImConMainController.py).
This matters because EtSnouty is **not** on `EventTriggeredControllerBase` and
will not inherit any handle (see §4).

The service should not know EtSnouty, EtSTED, or EtMonalisa internals. It should
only know workflow names and roles.

### 2. Non-Interactive Safety Policy

Interactive setup-mode application can ask the user about hazards. Event-time
application must not.

The hazard *detection* primitive already exists in the backend:
`SetupModeController.getModeHazards(stateByComponent, applyMode, context)`
([`SetupModeController.py:285`](../../../imswitch/imcontrol/controller/SetupModeController.py)).
The only thing that lives in UI code is the *confirmation gate*
(`self._widget.confirmHighPowerApply(...)`,
[`SetupModesController.py:442`](../../../imswitch/imcontrol/controller/controllers/SetupModesController.py))
plus the reading of threshold/suppression settings. So this phase is **not** an
extraction of detection logic — it is a thin non-interactive policy that reuses
`getModeHazards` and replaces the dialog with a decision.

Policy:

- preflight hazards at experiment arming time for all configured role modes
  (call `getModeHazards` for each resolved mode before the loop starts);
- block arming if an automated transition would require confirmation;
- allow explicit per-mode or per-workflow opt-in only through setup
  configuration, not a transient event-time dialog;
- log every role application and warning in the event log;
- on role-apply failure, stop or recover to `idle` rather than continuing with
  stale hardware state.

**Failure contract (important).** `loadSetupMode` does *not* raise on partial
failure: it accumulates per-component problems into a returned `warnings` list
and continues
([`SetupModeController.py:217-250`](../../../imswitch/imcontrol/controller/SetupModeController.py)).
A flip mirror that fails to move surfaces only as a warning string, not an
exception — and a silently skipped `FlipMirror` before a high-resolution scan is
a real safety event.

Rather than parse warning strings (brittle), expose the failure structurally.
The backend's per-component apply loop already knows which component *raised*:
the `except Exception` branch
([`SetupModeController.py:241`](../../../imswitch/imcontrol/controller/SetupModeController.py))
is the unambiguous catastrophic-failure signal, distinct from the recoverable
warnings a component returns by contract
([`StatefulComponentMixin.applyComponentState`](../../../imswitch/imcontrol/controller/basecontrollers.py),
"MUST NOT raise for recoverable issues; MAY raise for catastrophic errors").
So add a structured backend entry point:

- `SetupModeController.applySetupMode(name, componentNames=None) -> ApplyOutcome`
  returning `warnings: list[str]` (identical strings/order to today) **and**
  `failedComponents: list[str]` (components whose apply raised);
- `loadSetupMode` becomes a thin wrapper returning `outcome.warnings`, so the
  interactive controller and existing tests are unchanged.

The service's `ApplyResult.ok` is then `False` iff a component whose controller
declares `setupModeHardwareCritical = True` is in `failedComponents` or
`warningComponents`. Non-hardware warnings are surfaced for logging but do not
by themselves block; a beam-path apply exception or warning does. This is
metadata-driven, not string-driven.

This keeps high-power laser and beam-path changes auditable before the event
loop is running.

### 3. Event Controller Integration

Extend `EventTriggeredControllerBase` with optional mode roles:

- during `initiate`, after `_prepareExperiment` and before enabling the fast
  laser: apply `scouting`;
- during `pauseFastModality`, after disconnecting image signals and disabling
  the fast laser: apply `event`;
- during `continueFastModality`, before reconnecting image signals and
  re-enabling the fast laser: apply `resume` or `scouting`;
- during `stopExperiment` and failure recovery: apply `idle` if configured.

The insertion points map onto existing hooks: `scouting` fits inside
`_pre_arm_hook` (runs after `_prepareExperiment`, before `_connectRunSignals`
and `_setFastLaserEnabled(True)`,
[`EventTriggeredBaseController.py:226-229`](../../../imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py));
`event` fits inside `_on_pause_modality_hook`; `resume` inside
`_on_resume_modality_hook`; `idle` inside `_post_stop_hook`.

Keep subclass hooks. They remain useful for workflow-specific actions that are
not declarative setup modes yet. The target is to shrink those hooks over time:
EtMonalisa stand switching should become setup-mode state, and EtSnouty mirror
switching should become setup-mode state.

**Laser ownership is a hard rule, not a guideline.** Today EtSnouty's
`setConfig` both moves mirrors *and* toggles the fast laser, and the laser is
re-toggled in several other places: `runSlowScan` re-enables it
([`EtSnoutyController.py:269`](../../../imswitch/imcontrol/controller/controllers/EtSnoutyController.py)),
`clockWidefield_fct` flips it per frame
([`EtSnoutyController.py:499-502`](../../../imswitch/imcontrol/controller/controllers/EtSnoutyController.py)).
If a `scouting`/`event` setup mode also carries `Laser` state, the mode apply
and the controller will fight over emission. **Initial scouting/event/resume
modes contain beam-path and viewer state only (`FlipMirror`, and the
tilted/straight detector visibility); the event controller keeps owning
fast-laser emission.** Folding laser state into modes is a later, deliberate
step taken per workflow once the contract for who enables emission is explicit
(see Risks). `idle` is the exception: it may set lasers to a known safe state
because no controller is contending for them at stop time.

### 4. EtSnouty Port

Port EtSnouty in two steps.

First, replace the hard-coded `sigSetConfig` calls with role applications:

- `setConfig(widefield=True)` becomes apply `scouting`;
- `setConfig(widefield=False)` becomes apply `event`;
- the visible-layer update splits in two: EtSnouty **already** emits
  `sigSetVisibleLayers((self.detectorFast,))` for the fast detector itself
  ([`EtSnoutyController.py:241`](../../../imswitch/imcontrol/controller/controllers/EtSnoutyController.py)),
  so that path survives `SetupStatus` removal unchanged. Only the mirror-driven
  tilted/straight layer emitted from `SetupStatus`
  ([`SetupStatusController.py:195-197`](../../../imswitch/imcontrol/controller/controllers/SetupStatusController.py))
  needs a new home — see Open Decision 1. This means removing `SetupStatus` does
  not strand the fast-detector display.

The service handle reaches EtSnouty via the `setSmartModeService(...)` setter
(§1), since EtSnouty is not on the base class. To de-risk the live cutover, a
per-workflow config flag selects the new service path vs. the legacy
`sigSetConfig` emission, so a lab can roll back without reverting code while
external setups migrate.

Second, move EtSnouty toward `EventTriggeredControllerBase`. That can be a later
refactor because the first step removes the setup-status dependency without
rewriting the whole controller.

### 5. SetupStatus Decommission

**Repository reality.** No setup JSON in the repo references `SetupStatus`,
`EtSnouty`, the KDC stage, or `rotationStage` — the canonical Snouty setup lives
in an external lab checkout. So this decommission, and Phase 4's acceptance
criteria, are partly a lab-side migration that CI cannot exercise directly. To
keep the work testable in-repo, add a minimal fixture setup (fake `FlipMirror`,
fake fast detector, the three modes) that the role-switching and EtSnouty tests
can boot against.

After EtSnouty no longer depends on `sigSetConfig`:

- remove `SetupStatus` from the Snouty setup's `availableWidgets` and layout;
- replace the KDC rotation-stage entry with `KDC101PositionerManager` under
  `positioners`;
- ensure `Positioner` and `SetupModes` are available in the Snouty setup;
- optionally leave `SetupStatusController` in-tree temporarily for old setups,
  but mark `sigSetConfig` as deprecated compatibility.

Do not delete `SetupStatusController` until any lab setup files outside the repo
have been migrated or fail with a clear message.

## Setup Mode Content For Snouty

The Snouty setup should define at least three modes:

### Snouty widefield scouting

Expected components:

- flip mirrors set to straight widefield detection/illumination;
- fast detector visible;
- **no `Laser` component initially** — EtSnouty owns fast-laser emission (§3);
  fold laser state in only after the ownership contract is explicit;
- optional camera settings;
- optional scan settings if the scouting path needs a known scan state.

### Snouty light-sheet event scan

Expected components:

- flip mirrors set to light-sheet illumination and tilted detection;
- slow/event acquisition settings;
- any SLM/scan settings required before triggering the slow scan;
- laser state should be handled carefully: if the scan controller owns laser
  timing, the setup mode should not fight it.

### Snouty safe idle

Expected components:

- lasers disabled or set to known safe low-power values;
- shutters/mirrors in the safest resting state for the setup;
- no stage/positioner movement unless explicitly reviewed.

## Open Design Decisions

1. Viewer state:
   Scope is smaller than it first looks. EtSnouty already drives the
   fast-detector layer itself, so only the mirror-driven tilted/straight layer
   from `SetupStatus` needs a home (§4). Decide whether that one mapping belongs
   in setup modes via a new passive `ViewerState` component, or whether event
   controllers keep a small display-only role mapping. Prefer the latter for the
   first migration; promote to a `ViewerState` component only if a second
   workflow needs it.

2. Mode storage:
   Start with `smartMicroscopyModes` in setup JSON. Later, consider role tags
   inside setup mode files if the UI should expose "assign this mode as
   scouting/event/idle".

3. Hazard policy UI:
   Add a preflight dialog when arming smart microscopy workflows, not during
   event detection. The dialog should list every role mode that will be applied
   automatically.

4. EtSnouty modernization:
   Decide whether to only replace mode switching now or also port EtSnouty to
   `EventTriggeredControllerBase`. The safer order is mode switching first,
   base-class port second.

5. Legacy `sigSetConfig`:
   Keep as deprecated compatibility until no setup uses `SetupStatus`; then
   remove `sigSetConfig` and `sigSetVisibleLayers` from `CommunicationChannel`.
   Both are declared at
   [`CommunicationChannel.py:61-62`](../../../imswitch/imcontrol/controller/CommunicationChannel.py)
   and covered by the signal-inventory contract test
   (`communication_channel_signal_inventory.json`), so removal must update that
   inventory and `test_setup_status_controller.py` in the same change.

## Implementation Phases

### Phase 1: Role Mapping And Service — Implemented

- `SetupInfo` now has `smartMicroscopyModes`,
  `smartMicroscopyModePolicies`, and
  `smartMicroscopyModeSwitchingEnabled`.
- `SmartMicroscopyModeService` depends only on `SetupModeController` and setup
  role config — no widget and no dependency on `SetupModesController`.
- `SetupModeController.getLastAppliedModeName()` is the shared de-dup source for
  interactive and non-interactive mode application.
- Unit tests cover role resolution, missing mode validation, duplicate role
  no-op, warning propagation, failed mode apply, and recovery reapply after a
  failed transition.
- `ImConMainController` constructs the service and passes it to controllers that
  expose `setSmartModeService(...)`.

Acceptance criteria:

- no hardware is touched by service tests;
- service can validate and apply named setup modes through a fake
  `SetupModeController`, returning warnings synchronously;
- applying a role whose resolved mode is already active is a no-op;
- missing role modes are reported before an experiment arms;
- a failed mode transition clears the active-mode marker so recovery can
  reapply a previously clean mode.

### Phase 2: Backend Safety — Implemented

- Add `applySetupMode(name) -> ApplyOutcome` to the backend (warnings +
  `failedComponents` + `warningComponents`); make `loadSetupMode` a thin
  wrapper over it (§2). Existing `loadSetupMode` callers/tests stay unchanged.
- Build the non-interactive policy in the service over the existing backend
  `getModeHazards` primitive — do not extract detection logic, it is already in
  the backend (§2). Only the dialog gate stays in `SetupModesController`.
- Add a `SmartModeHazardPolicy` enum (`ALLOW`, `WARN_ONLY`, `BLOCK_ON_HAZARD`
  default) and an optional per-workflow `smartMicroscopyModePolicies` field on
  `SetupInfo` (workflow → policy name), mirroring how `smartMicroscopyModes` was
  added. Unlisted workflows default to `BLOCK_ON_HAZARD`.
- Implement `SmartMicroscopyModeService.preflight(workflowName, roles=None) ->
  PreflightResult` (`ok`, `hazards`, `missingModes`, `failedModes`,
  `messages`): resolve roles, fetch each mode's `state` via `getSetupMode`,
  aggregate `getModeHazards`, apply policy. Under `BLOCK_ON_HAZARD`, `ok` is
  `False` if any hazard, missing mode, or unreadable mode is found.
- Upgrade `applyRole` to return `ApplyResult` and set `ok=False` when a
  hardware component either raises or returns component-level warnings during
  apply (§2). Update the Phase 1 tests that asserted the bare-list return.

Acceptance criteria:

- event-time role application never opens a modal dialog (the service holds no
  widget — assert it never calls a confirm/dialog path);
- `preflight` reports hazards and missing modes before arming, and blocks under
  `BLOCK_ON_HAZARD`;
- a hardware-component apply exception or component warning yields
  `ApplyResult.ok == False` and names the component, rather than being swallowed
  as a recoverable warning;
- `applySetupMode` returns the same warning strings/order as before for existing
  `loadSetupMode` callers (no regression in `test_setup_modes.py`);
- a partial/failed setup-mode apply does not leave
  `getLastAppliedModeName()` pointing at stale hardware state.

### Phase 3: EtSnouty Mode Replacement — Implemented Behind Rollout Flag

- Replace `EtSnoutyController.setConfig` mirror/config emission with service
  role applications, behind `smartMicroscopyModeSwitchingEnabled["EtSnouty"]`
  so the legacy `sigSetConfig` path stays available during rollout.
- Keep EtSnouty's explicit fast-laser enable/disable calls exactly as they are —
  the role modes do not carry `Laser` state (§3).
- Keep method names temporarily so the rest of EtSnouty changes minimally.
- Remove the dependency on `sigSetConfig` once the flag defaults to the service.
- Log role transitions in the Snouty event log.

Acceptance criteria:

- EtSnouty applies `scouting` on start;
- EtSnouty applies `event` exactly once when a detected event triggers a slow
  scan;
- EtSnouty applies `resume`/`scouting` after scan end when endless mode is
  enabled;
- EtSnouty applies `idle` on stop when that optional role is configured;
- EtSnouty applies `idle` and stops acquisition when required role application
  fails, including endless-mode event failure;
- fast-laser emission is still owned by EtSnouty, not by the applied mode;
- no per-frame setup-mode application occurs.

### Phase 4: Snouty Setup Migration — In-Repo Parts Implemented

In-repo (done): `imswitch/_data/user_defaults/imcontrol_setups/example_snouty_smart_modes.json`
is a no-hardware fixture (mock `ThorlabsMFF_mock` flip-mirror beam path, AVManager
mock fast detector, the KDC rotation stage as a `KDC101PositionerManager`
positioner, and the `Positioner`/`SetupModes`/`FlipMirror` widgets — no
`SetupStatus`). It is auto-booted by `test_example_setups.py`. The flip-mirror
switching path is covered by `test_snouty_smartmodes_flip_switching.py`, which
drives `applyRole` through the real `SetupModeController` into mock flip mirrors
with no event controller running. The external lab setup migration (below)
remains out-of-repo.

- Add an in-repo fixture setup (fake `FlipMirror` + fast detector + three modes)
  so Phases 3-4 are CI-testable; the canonical Snouty setup is external (§5).
- In the external lab setup: move the KDC stage into `positioners` using
  `KDC101PositionerManager`.
- Enable `Positioner`, `SetupModes`, and any hardware widgets needed to author
  the modes.
- Remove `SetupStatus` from the Snouty setup.
- Create the three Snouty setup modes: scouting, event, idle.

Acceptance criteria:

- the fixture setup boots without `SetupStatus` in CI;
- the lab Snouty setup boots without `SetupStatus`;
- the KDC stage is controllable from `Positioner`;
- setup mode apply can switch the relevant flip mirrors without EtSnouty
  running;
- EtSnouty event loop can run without `sigSetConfig`.

### Phase 5: Shared Base Integration — Implemented

Done: the role-switching glue is a shared `SmartModeRoleMixin`
(`controller/controllers/SmartModeRoleMixin.py`) used by both EtSnouty
(refactored off its Phase 3 duplicates) and `EventTriggeredControllerBase`. The
base wires role application into the lifecycle (scouting before laser-on at arm;
event at the top of `initiateSlowScan`, failure → idle + scan skipped; resume in
the endless-continue branch; idle on stop), each guarded by
`_smartModeSwitchingEnabled()` so a disabled workflow is byte-for-byte unchanged.
EtSTED sets `SMART_MODE_WORKFLOW='EtSTED'`; EtMonalisa sets `'EtMonalisa'`.
Tests: `test_event_triggered_smart_modes.py`.

### Phase 5b: LeicaStand as a Setup-Mode Component — Implemented

`LeicaStandController` is now a `StatefulComponentMixin` component
(`componentName='LeicaStand'`, ordering band `MICROSCOPE_STAND`). Snapshot is
**FLUO/CS mode only**; apply uses the **raw** `setFLUO`/`setCS`/`setILshutter`
sequence that mirrors EtMonalisa's hooks (behavior-preserving) but **surfaces
failures** instead of swallowing them via `_safe_call`. Scope is deliberately
mode-only: the real Leica DM6000 manager is external (only a 3-method mock is
in-repo), so cube/port/diaphragm can't be virtual-tested and would change
actuation — deferred to hardware. EtMonalisa's three Experiment-mode stand hooks
now gate the direct switch on `not _smartModeSwitchingEnabled()`: flag off keeps
the legacy direct switch byte-for-byte; flag on lets the `scouting`/`event`
setup-mode roles carry FLUO/CS (no double-actuation), gaining the failure
contract + preflight + logging. The `sigInitiateEtMonalisa` emission stays
unconditional. Tests: `test_leicastand_component_state.py`. Remaining: rewiring
EtMonalisa fully and authoring the FLUO/CS mode content is gated on hardware
validation.

- Add optional role hooks to `EventTriggeredControllerBase`.
- Port EtSTED and EtMonalisa to the same service where practical.
- Replace EtMonalisa direct stand commands with setup modes once Leica stand
  state is fully represented by `LeicaStandController`.

Acceptance criteria:

- EtSTED, EtMonalisa, and EtSnouty use one role-switching abstraction;
- direct hardware mode commands remain only where no setup-mode component exists;
- each workflow can specify different scouting/event/resume/idle modes.

### Phase 6: UI Authoring — Implemented

- Done: extend `SetupModesWidget` with smart-microscopy role assignment:
  "Use selected mode as scouting/event/resume/idle for workflow X".
- Done: show configured workflow role mappings in the event-triggered widget.
- Done: keep normal setup-mode authoring unchanged.

Acceptance criteria:

- users can assign roles without editing JSON by hand;
- role assignments are visible before arming;
- invalid or missing role modes are shown in the UI.

Status:

- covered by `test_setup_modes_controller_smart_roles.py`,
  `test_smart_microscopy_mode_service.py`, and
  `test_event_triggered_smart_modes.py`;
- hardware-specific role contents still need validation in the external Snouty
  setup.

## Test Plan

Unit tests:

- role mapping resolution;
- missing/renamed setup mode handling;
- preflight hazard behavior;
- duplicate role suppression;
- apply failure recovery;
- EtSnouty transition call counts for start, event, scan-ended resume, stop;
- EventTriggeredControllerBase role hook ordering.

No-hardware integration tests:

- fake setup modes with fake `FlipMirror`, `Laser`, and `LeicaStand`
  controllers;
- EtSnouty starts in scouting mode and switches to event mode on synthetic
  detection;
- EtMonalisa can express its FLUO/CS transition as setup modes once stand state
  support is complete.

Manual hardware validation:

- verify Snouty scouting mode mirror path;
- verify Snouty event light-sheet path;
- verify fast laser and scan laser ownership do not conflict;
- verify recovery from failed scan preparation returns to a safe configured
  mode;
- verify repeated events do not double-apply modes while already in that role.

## Risks

- Setup modes can activate hardware. Automated application must be explicitly
  preflighted and logged.
- `loadSetupMode` reports component failures as warnings, not exceptions, and
  keeps going. The service must treat hardware-component warnings as failures or
  a silently skipped flip mirror will be invisible (see §2 failure contract).
- Laser ownership may be split between setup modes, event controllers, and scan
  presets. Each workflow needs a clear contract for who enables emission; the
  initial migration sidesteps this by keeping `Laser` out of scouting/event
  modes.
- Viewer-layer visibility is not currently a setup-mode component. Only the
  mirror-driven tilted/straight layer is affected — EtSnouty already owns the
  fast-detector layer — so the gap is narrow but real.
- EtSnouty is not yet on the shared event-triggered base, so the first migration
  should be narrow.
- External lab setup files may still list `SetupStatus`; keep compatibility
  until migration is verified.

## Recommendation

Use setup modes as the state source of truth, but do not make event controllers
select arbitrary UI modes directly. Add a role-based transition service so each
smart microscopy workflow declares which setup mode corresponds to scouting,
event acquisition, resume, and idle. Then migrate EtSnouty off `sigSetConfig`
first, because that is the blocker for removing `SetupStatus` from the Snouty
setup.

**Sequencing.** Phases 1-6 are implemented in-repo and covered by focused
software tests. Keep the EtSnouty rollback flag available until the external
Snouty setup has migrated. The remaining required step is lab-side validation:
verify the Snouty setup modes, move the KDC stage into `Positioner`, remove
`SetupStatus` from the external setup, and run manual hardware checks for mirror
paths, laser ownership, recovery, and repeated-event behavior.

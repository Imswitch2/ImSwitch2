# EtMonalisa 2.0 Plan

## Goals

EtMonalisa 2.0 should bring the MoNaLISA event-triggered workflow to the same safety and testability level as the EtSTED 2.0 controller work:

- detect events from a fast modality,
- switch/pause the fast modality safely,
- transform event coordinates,
- trigger a slow/high-resolution scan,
- record enough metadata to audit the event,
- recover predictably on stop, failure, or application close.

## Safety Boundaries

The EtMonalisa controller touches red-zone behavior:

- laser enable/disable,
- microscope stand mode and shutter control,
- scan initiation,
- galvo/stage center updates,
- detector live frame handling,
- recording trigger coordination.

Refactoring must not change hardware timing constants, scan waveform generation, laser powers, TTL settings, calibration constants, or microscope stand command order unless separately reviewed with hardware access.

## Phase 1: Safety And Runtime Hygiene

Scope:

- Replace Windows-only timing with portable monotonic timing.
- Remove NumPy 2 incompatible aliases.
- Ensure runtime log directories exist before writing.
- Add idempotent cleanup for stop and close:
  - disconnect signals best-effort,
  - unblock ScanWidget if it was blocked,
  - disable the selected fast laser,
  - reset running/busy state,
  - emit the EtMonalisa initiation state consistently.
- Validate that an analysis pipeline and coordinate transform are available before starting.
- Make slow-scan preparation return success/failure so stale scan state is not reused after generation errors.

Acceptance criteria:

- Import/compile of `EtMonalisaController.py` succeeds.
- `ruff` critical-error check passes for the controller.
- No hardware actions are added beyond existing start/stop behavior.
- Failures before or during initiation leave the fast laser disabled and UI state reset.

Status: implemented in the controller as the Phase 1 foundation for EtMonalisa 2.0.

## Phase 2: Session State Model

Introduce an explicit session object for:

- selected fast detector/laser,
- run mode,
- scan initiation mode,
- connected signal handles,
- busy/validating/frame counters,
- event log state.

This should remove scattered private flags and make stop/recovery paths deterministic.

Status: implemented for EtMonalisa via `EventTriggeredSessionState`, shared
run/scan enums, and controller contract tests. This gives EtSTED a concrete
shared model to adopt in the next de-duplication pass without changing
hardware timing or scan generation.

## Phase 3: Pipeline Runner

Reuse or generalize the EtSTED pipeline runner for:

- pipeline discovery,
- signature validation,
- typed result contract,
- parameter parsing and validation,
- failure handling that always clears busy state.

## Phase 4: Coordinate Transform Service

Reuse or generalize the EtSTED transform service for:

- explicit path-based transform loading,
- coefficient file validation,
- calibration metadata persistence,
- setup-driven axis swap/invert behavior where applicable,
- unit tests for transform direction and calibration serialization.

## Phase 5: Triggered Scan Runner

Reuse or generalize the EtSTED triggered scan runner for:

- scan parameter validation before arming,
- scan center mapping from loaded scan axes instead of hardcoded device names,
- ScanWidget and RecordingWidget initiation behind one interface,
- explicit success/failure reporting.

## Phase 6: UI Modernization

Update `EtMonalisaWidget` to make the state machine visible:

- explicit status indicator: idle, arming, detecting, triggered, scanning, error,
- disabled controls while armed where changing them would be unsafe,
- validation messages for missing pipeline/transform/scan parameters,
- no manual "Unlock softlock" as the primary recovery mechanism.

## Phase 7: No-Hardware Validation

Add mock-backed tests for:

- pipeline loading and parameter parsing,
- event detection path with synthetic images,
- no-event path,
- stop/close cleanup disables laser mock and emits EtMonalisa inactive state,
- scan preparation failure does not trigger scan,
- coordinate transform application.

These tests must not start physical hardware, DAQ tasks, laser emission, microscope stand commands, or stage movement.

## Phase 8: Shared Base Hardening

Status: implemented.

- `EventTriggeredControllerBase` now owns the shared EtSTED/EtMonalisa
  session state and cleanup contracts.
- The EtMonalisa controller contract verifies that modality-specific code stays
  thin and does not reintroduce duplicated private runtime flags.
- Interrupted binary-mask acquisition is explicitly cleaned up during stop and
  close paths.
- Fast-laser enable failures abort arming/resume instead of silently entering a
  detecting state.
- Pipeline coordinate outputs are normalized to `(N, 2)` and invalid shapes are
  rejected with a clear error.
- The focused event-triggered no-hardware suite passes without starting
  hardware, DAQ tasks, laser emission, microscope stand commands, or stage
  movement.

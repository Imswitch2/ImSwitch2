# EtSTED 2.0 Plan

## Goals

EtSTED 2.0 should turn the current lab-specific event-triggered workflow into a safer, testable, and more explicit event-triggered imaging subsystem. The main outcome is not a different microscope sequence, but a cleaner architecture around the existing sequence:

- detect events from a fast modality,
- pause the fast modality safely,
- transform event coordinates,
- trigger a slow/high-resolution scan,
- record enough metadata to audit the event,
- recover predictably on stop, failure, or application close.

## Safety Boundaries

The EtSTED controller touches red-zone behavior:

- laser enable/disable,
- scan initiation,
- galvo/stage center updates,
- detector live frame handling,
- recording trigger coordination.

Refactoring must not change hardware timing constants, scan waveform generation, laser powers, TTL settings, or calibration constants unless separately reviewed with hardware access.

## Phase 1: Safety And Runtime Hygiene

Scope:

- Replace Windows-only timing with portable monotonic timing.
- Remove NumPy 2 incompatible aliases.
- Ensure runtime log directories exist before writing.
- Add idempotent cleanup for stop and close:
  - disconnect signals best-effort,
  - unblock ScanWidget if it was blocked,
  - disable the fast laser if it was selected,
  - reset running/busy state.
- Validate that an analysis pipeline and coordinate transform are available before starting.
- Make slow-scan preparation return success/failure so stale scan state is not reused after generation errors.

Acceptance criteria:

- Import/compile of `EtSTEDController.py` succeeds.
- `ruff` critical-error check passes for the controller.
- No hardware actions are added beyond existing start/stop behavior.
- Failures before or during initiation leave the fast laser disabled and UI state reset.

Status: implemented in the controller as the Phase 1 foundation for EtSTED 2.0.

## Phase 2: Session State Model

Introduce an explicit session object for:

- selected fast detector/laser,
- run mode,
- scan initiation mode,
- connected signal handles,
- busy/validating/frame counters,
- event log state.

This should remove scattered private flags and make stop/recovery paths deterministic.

Acceptance criteria:

- Runtime flags are grouped in one controller-owned session state object.
- Stop, pause, signal connect/disconnect, and pipeline execution read/write the same state object.
- Existing selected detector/laser attributes remain mirrored for compatibility.
- Event log storage is part of session state.
- Focused no-hardware validation still passes.

Status: implemented in the controller as the Phase 2 state model.

## Phase 3: Pipeline Runner

Extract analysis pipeline loading and execution into a small service:

- pipeline discovery,
- signature validation,
- typed result contract,
- parameter parsing and validation,
- failure handling that always clears busy state.

The controller should not call arbitrary plugin functions directly from the Qt signal handler without a guarded boundary.

Acceptance criteria:

- Pipeline loading, signature validation, UI parameter parsing, and execution are handled by one runner object.
- Pipeline execution returns a normalized result object.
- Invalid signatures, invalid parameter values, and invalid return shapes fail before hardware scan triggering.
- The image signal handler clears busy state on runner failures.
- Unit tests cover the runner contract without requiring hardware.

Status: implemented in the controller as the Phase 3 pipeline runner.

## Phase 4: Coordinate Transform Service

Extract transform loading, calibration, and application:

- avoid mutating `sys.path` globally,
- validate coefficient file shape,
- persist calibration metadata,
- apply `SetupInfo.etSTED` axis swap/invert flags consistently,
- provide unit tests for transform direction and calibration serialization.

Acceptance criteria:

- Transform function loading is handled by a service using explicit file paths instead of adding the transform directory to `sys.path`.
- Coefficient files are validated as third-order transform vectors before use.
- Transform application supports `SetupInfo.etSTED` swap/invert flags before coordinate conversion.
- Calibration math is testable outside the Qt helper.
- Unit tests cover coefficient validation, transform application, setup flags, and calibration behavior.

Status: implemented in the controller as the Phase 4 coordinate transform service.

## Phase 5: Triggered Scan Runner

Extract slow-scan preparation and triggering:

- validate scan parameters before arming EtSTED,
- make scan center device mapping setup-driven instead of hardcoded `ND-GalvoX` / `ND-GalvoY`,
- distinguish scan-widget and recording-widget initiation behind one interface,
- report whether scan preparation and trigger succeeded.

Acceptance criteria:

- Slow-scan preparation and scan triggering are handled by one runner object.
- Loaded scan parameters are validated before arming experiment mode.
- Event coordinates map to the first two active scan axes from the loaded scan parameters instead of hardcoded device names.
- ScanWidget and RecordingWidget initiation paths return explicit success/failure results.
- Failed scan preparation or triggering clears busy state and returns to the fast-modality recovery path.
- Unit tests cover scan parameter validation, axis mapping, ScanWidget preparation/triggering, and RecordingWidget preparation/triggering.

Status: implemented in the controller as the Phase 5 triggered scan runner.

## Phase 6: UI Modernization

Update `EtSTEDWidget` to make the state machine visible:

- explicit status indicator: idle, armed, detecting, triggered, scanning, error,
- disabled controls while armed where changing them would be unsafe,
- validation messages for missing pipeline/transform/scan parameters,
- no manual "Unlock softlock" as the primary recovery mechanism.

Acceptance criteria:

- Widget exposes explicit runtime states: idle, arming, detecting, triggered, scanning, and error.
- Controller updates the status surface during arming, detection, triggering, scan execution, and failures.
- Controls that would invalidate an armed run are disabled while EtSTED is armed.
- Validation and runner failures are surfaced as status messages instead of relying on console/log output only.
- The softlock button is hidden during normal operation and shown only for error recovery.

Status: implemented in the controller/widget as the Phase 6 UI state surface.

## Phase 7: No-Hardware Validation

Add mock-backed tests for:

- pipeline loading and parameter parsing,
- event detection path with synthetic images,
- no-event path,
- stop/close cleanup disables laser mock,
- scan preparation failure does not trigger scan,
- coordinate transform application.

These tests must not start physical hardware, DAQ tasks, laser emission, or stage movement.

Acceptance criteria:

- Synthetic event images exercise pipeline execution, coordinate transform application, scan preparation, and scan triggering using only mocks.
- Synthetic no-event images do not prepare or trigger scans.
- Scan preparation failure does not trigger the mock DAQ path.
- Stop/close cleanup keeps an explicit contract that disconnects signals and disables the selected fast laser.
- The focused no-hardware EtSTED test suite runs without importing napari widgets or starting physical managers.

Status: implemented as the Phase 7 no-hardware validation suite.

## Phase 8: Shared Base Hardening

Status: implemented.

- `EventTriggeredControllerBase` now owns the shared EtSTED/EtMonalisa
  session state and cleanup contracts.
- Interrupted binary-mask acquisition is explicitly cleaned up during stop and
  close paths: the image signal is disconnected, the temporary frame stack is
  cleared, and the record button text is restored.
- Fast-laser enable failures abort arming/resume instead of silently entering a
  detecting state.
- Pipeline coordinate outputs are normalized to `(N, 2)` and invalid shapes are
  rejected with a clear error.
- No-hardware tests cover the shared state contract, pipeline coordinate
  normalization, cleanup contracts, and the focused event-triggered workflow.

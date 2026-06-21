# Workflow Readiness Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Scope:** `imswitch/imcontrol/model/workflows/**/*.py`, with emphasis on tiling,
target timelapse, setup modes, and event-gated acquisition.

## Summary

The new workflow layer is directionally good: `TargetTimelapseWorkflow`,
`EventProbe`, and `EventGatedAcquisitionCallback` are model-level, callback
driven, and mostly independent of Qt widgets. This is the right foundation for
"overview -> decide targets -> revisit targets -> probe event -> trigger
acquisition" workflows.

The remaining problems are mostly production-readiness issues: cleanup on
failure, cancellation, controller-owned mode protocol types, and a few
backward-compatible defaults that still encode one setup's assumptions.

## Findings

### [P1] Tiling workflow does not guarantee cleanup on failure

**Sites:**

- `imswitch/imcontrol/model/workflows/tiling.py:225-230`
- `imswitch/imcontrol/model/workflows/tiling.py:245-258`
- `imswitch/imcontrol/model/workflows/tiling.py:261-312`
- `imswitch/imcontrol/model/workflows/tiling.py:314-318`
- `imswitch/imcontrol/model/workflows/tiling.py:480-490`

**Evidence:**

`TilingWorkflow.run()` opens an HDF5 file, configures laser trigger/constant
power mode, then enters the tile loop. Laser cleanup and HDF5 close happen only
after the loop completes successfully. `_grab_image()` turns pulsed lasers on
before camera operations and turns them off only after `get_data()` succeeds.

**Impact:**

If a stage move, camera call, HDF5 write, tile callback, or segmentation path
raises, the workflow can leave the laser in the wrong mode, leave pulsed lasers
on, leak an HDF5 handle, or skip the expected final stage move. This is the most
important workflow issue before combining tiling with modes and event-triggered
acquisition.

**Next fix:**

Add cleanup guards around workflow run and image acquisition:

- Use `try/finally` or context managers to close HDF5 files.
- Restore laser modulation/off state in a `finally` block.
- Stop camera live/acquisition in a `finally` block.
- Make final stage positioning explicit and optional, and record whether it ran.
- Add failure-path tests with fake facade components that raise mid-run.

### [P1] Workflow mode contracts still point back to controller services

**Sites:**

- `imswitch/imcontrol/model/workflows/smart_mode_workflow.py:35`
- `imswitch/imcontrol/model/workflows/target_timelapse.py:40`
- `imswitch/imcontrol/model/workflows/target_timelapse.py:196`
- `imswitch/imcontrol/model/workflows/target_timelapse.py:320-324`

**Evidence:**

The workflow layer references `SmartMicroscopyModeService` and result classes
from the controller package under `TYPE_CHECKING`, and
`TargetTimelapseWorkflow` accepts a `SmartModeWorkflowAdapter`.

**Impact:**

The runtime behavior is already injected, which is good. The remaining type
ownership is still backwards: workflows should define the mode-applier protocol,
and controller services should implement it. Otherwise headless workflows and
external workflow packages need to know controller module names.

**Next fix:**

Move mode result dataclasses and a `ModeRoleApplier` protocol into
`model/workflows` or `imcommon`. Let `SmartMicroscopyModeService` adapt to that
protocol instead of being the protocol owner.

### [P1] Workflow cancellation is not yet a first-class concept

**Sites:**

- `imswitch/imcontrol/model/workflows/target_timelapse.py:350-353`
- `imswitch/imcontrol/model/workflows/target_timelapse.py:415-417`
- `imswitch/imcontrol/model/workflows/tiling.py:261-312`
- `imswitch/imcontrol/model/workflows/event_probe.py:159-190`

**Evidence:**

Target timelapse can inject `sleep_fn`, which is good for tests, but there is no
cancellation token checked between target moves, during long interval waits, or
during event probing. Tiling loops over all tiles without a stop predicate.
Event probing loops over frames until `max_frames` or `StopIteration`.

**Impact:**

Long-running workflows need a common way to stop from the UI, automation layer,
or emergency safety path. Without it, each controller will grow its own stop
flag and cleanup behavior again.

**Next fix:**

Introduce a small workflow runtime context:

- `should_stop()` or cancellation token checked at stable boundaries.
- `sleep(seconds)` that can wake early on cancellation.
- `progress(event)` callback for UI/status updates.
- `cleanup_stack` or device guard helpers for hardware cleanup.

### [P2] Tiling still defaults to one legacy channel shape

**Sites:**

- `imswitch/imcontrol/model/workflows/tiling.py:63`
- `imswitch/imcontrol/model/workflows/tiling.py:88`
- `imswitch/imcontrol/model/workflows/tiling.py:89`

**Evidence:**

`TilingParams` now supports `laser_names` and per-channel powers, but the
backward-compatible defaults still name the old 488 nm channel and default power.

**Impact:**

This is acceptable for compatibility, but not ideal as the visible default for a
general workflow. It keeps example-specific terminology in a reusable workflow
API.

**Next fix:**

Keep the fields for compatibility, but add a setup-derived factory such as
`TilingParams.from_setup_defaults(...)` and make UI/config code supply explicit
laser names. Document that bare `TilingParams(n_tiles=...)` is a legacy
compatibility shortcut.

### [P2] Measurement output roots are only partly policy-driven

**Sites:**

- `imswitch/imcontrol/model/workflows/paths.py:10-17`
- `imswitch/imcontrol/model/workflows/tiling.py:97`
- `imswitch/imcontrol/model/workflows/tiling.py:330-334`

**Evidence:**

The path helper supports `IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT`, which is good,
but Windows still falls back to `D:/Measurements` and tiling captures the module
default at import time in `DEFAULT_MEASUREMENTS_ROOT`.

**Impact:**

Output policy should be predictable in tests and deployments. Import-time
defaults can surprise callers that change environment variables after import.
The `D:/Measurements` default is probably useful for one historical setup, but
not universal.

**Next fix:**

Resolve defaults at workflow construction time, not module import time. Prefer a
setup/application measurements-root setting, then environment variable, then a
platform-neutral user directory.

### [P2] Event probing is reusable but too loosely typed at the detector boundary

**Sites:**

- `imswitch/imcontrol/model/workflows/event_probe.py:122-127`
- `imswitch/imcontrol/model/workflows/event_probe.py:174-178`
- `imswitch/imcontrol/model/workflows/event_probe.py:300-333`

**Evidence:**

`EventProbe` expects detector callbacks to return a dict containing
`coords_detected` and optional metadata. It normalizes coordinate arrays well,
but the detector result itself is not a dataclass or protocol.

**Impact:**

This is fine for prototyping, but a public workflow extension point should make
pipeline outputs explicit. External event-triggered implementations will
otherwise rely on string keys and undocumented optional fields.

**Next fix:**

Introduce an `EventDetectionResult` dataclass or protocol and accept dicts only
through a compatibility adapter.

## Suggested sequencing

1. Add tiling workflow cleanup guards and failure-path tests.
2. Introduce a shared workflow runtime context for cancellation, sleep, progress,
   and cleanup.
3. Move smart-mode workflow protocols out of controller-owned modules.
4. Replace string-key event detector dicts with typed result objects plus a dict
   compatibility adapter.
5. Make workflow defaults come from setup/application configuration instead of
   historical channel/path assumptions.

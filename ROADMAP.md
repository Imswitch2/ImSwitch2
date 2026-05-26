# ImSwitch2 Migration Roadmap

This roadmap tracks the major milestones for the ImSwitch2 migration. Each milestone should be created as a GitHub Milestone with associated issues.

## Milestone 1: Stabilization ✅

**Goal:** Establish a stable, tested baseline from the imported ImSwitch code.

- ✅ Import existing ImSwitch codebase and tag baseline
- ✅ Set up CI pipeline (linting, tests, build)
- ✅ Critical lint errors resolved (`E9/F63/F7/F82` clean)
- ✅ First unit tests passing in CI (`test_stores.py`)
- ✅ Add first no-hardware validation profile
  - `example_no_hardware.json` uses mock camera + mock positioners, no lasers,
    no RS232 devices, no physical DAQ channels, and `nidaq.simulation=true`
  - `imswitch/test_no_hardware_profile.py` verifies config parsing, absence of
    physical IO channels, and core manager construction
  - CI job: "No-Hardware Validation"
- ✅ Restore legacy no-hardware unit-test collection and expand CI coverage
  - Decoupled `imswitch/imcontrol/_test` model fixtures from GUI imports
  - CI now runs `imswitch/test_no_hardware_profile.py` plus the full
    `imswitch/imcontrol/_test/unit` directory
  - Qt-backed no-hardware unit tests run with explicit `pytest-qt` loading and
    third-party pytest plugin autoload disabled
- ✅ Ensure the full application UI launches without errors on a clean install
  using a no-hardware setup
  - Manual no-hardware launch regressions found during widget/layout work were
    fixed, including the empty-main-window regression
  - `imswitch/test_no_hardware_ui_smoke.py` now constructs the ImControl
    view/controller graph with the no-hardware setup under Qt offscreen mode
  - The smoke test isolates napari/vispy/matplotlib/colour dependencies so
    incompatible local GUI stacks do not block no-hardware startup validation

## Milestone 2: Packaging Cleanup 🔄

**Goal:** Clean up the project structure and packaging for easy installation.

- ✅ Modernize `setup.cfg`: `python_requires = >=3.10`, relaxed version pins
- ✅ Split hardware packages into optional extras (`[hardware]`, `[full]`)
- ✅ Core install (`pip install imswitch`) no longer requires NI-DAQ, Lantz, napari
- ✅ Consolidate or remove `setup.py` in favour of `pyproject.toml` only (deleted in Milestone 3)
- ⬜ Remove dead code and unused imports (29 bare `except:` blocks remain in `imcontrol/`)
- ⬜ Establish clear package boundaries

## Milestone 3: Code-Level Bug Fixes

**Goal:** Resolve known structural bugs and naming inconsistencies found during codebase analysis.

- ✅ Fix duplicate `EtSTEDInfo` class in `imswitch/imcontrol/model/SetupInfo.py` (renamed first to `EtSTEDDeviceInfo`)
- ✅ Rename `SQUIDLaserManager.py` → `ESP32LEDLaserManager.py` to match the class it contains
- ✅ Delete redundant `setup.py` shim (build metadata fully covered by `setup.cfg` + `pyproject.toml`)
- ✅ Remove dead `SLMController`/`slmManager` layer (verified gone from `MasterController`; superseded by `SLMsController`/`slmsManager`)
- ✅ Move `__test_Manager.py` from `imcontrol/` root into `_test/` (now at `imcontrol/_test/__test_Manager.py`; still requires live hardware, still not part of CI)
- ⬜ Separate driver mocks from real interfaces in `model/interfaces/` (currently mixed with no clear pattern)
- ⬜ Remove bare `except:` blocks and blank imports (deferred from Milestone 2 cleanup)

## Milestone 3b: ImControl Backbone Cleanup 🔄

**Goal:** Reduce coupling in the ImControl communication backbone without
breaking existing controller/API signal contracts.

- ✅ Add `CommunicationChannel` signal inventory contract tests
  - Snapshot covers all signal names and signatures
  - Public API signal aliases remain guarded
- ✅ Reorganize `CommunicationChannel` into domain sections
  - Acquisition/image, view, recording, scan, snapshot, SLM, focus/rotation,
    event-triggered, bead-recognition, useq, scripting
  - Duplicate `getNumCamTTL()` helper removed
- ✅ Mark unused compatibility signals explicitly
  - Deprecated but still available: `sigGridToggled`, `sigCrosshairToggled`,
    `sigScanFrameFinished`, `sigClockWidefield`
- ✅ Add domain event aliases
  - `scanEvents`, `recordingEvents`, `eventTriggeredEvents`, `beadRecEvents`,
    and related read-only groups preserve legacy `sigX` attributes
- ✅ Introduce workflow services for multi-step coordination
  - `ScanWorkflowService`: scan parameter requests, scan-start notifications,
    recording-triggered scan coordination, axis-center updates
  - `BeadRecWorkflowService`: MoNaLISA/bead-recognition center-query and
    axial-list coordination
  - EtSTED, EtMonalisa, RecordingController, ScanControllerMoNaLISA,
    ScanControllerAdvanced, and BeadRecController now route selected workflow
    interactions through these services
  - Covered by `test_workflow_services.py` and communication-channel contract
    tests
- ⬜ Continue extracting remaining broad signal usage where it is covered by
  no-hardware tests
- ⬜ Remove deprecated signals only after public API compatibility review
  and representative no-hardware startup/widget-set tests

## Milestone 4: Hardware Abstraction Cleanup 🔄

**Goal:** Improve the hardware abstraction layer for clarity and safety.

- 🔄 Document hardware interfaces
  - Architecture manager inventory and device reference pages now cover the
    active manager layer
  - Positioner docs include Kinesis shutdown/reset behavior and raw-driver-unit
    scaling guidance
  - Remaining work: fill gaps for older interfaces and mock/real separation
- 🔄 Standardize manager/controller patterns
  - Workflow services and no-hardware workflow facades reduce broad manager and
    communication-channel coupling in selected paths
  - Remaining work: apply the pattern gradually to older managers/controllers
- 🔄 Add type hints to hardware interfaces
  - New and touched workflow/manager helpers use typed dataclasses or typed
    method signatures
  - Remaining work: older manager/interface modules are still inconsistent
- 🔄 Improve error handling in hardware communication
  - Cobolt 06-01 pause fallback handles firmware that rejects `las:paus`
  - Jena/Kinesis setup configuration issues are documented and no-hardware
    contract tested
  - Remaining work: broader serial/driver error taxonomy
- ⬜ Add timeout mechanisms where missing

## Milestone 5: Detector Manager Refactor 🔄

**Goal:** Modernize and simplify detector management.

- ✅ Map current detector manager dependencies
  - Captured in `docs/design/ARCHITECTURE.md` and detector reference docs
- 🔄 Define clean detector interface
  - Existing detector manager contract is documented; a formal narrow facade is
    still pending
- 🔄 Refactor with backward compatibility
  - Detector state persistence and selected detector fixes have landed without
    breaking current controller APIs
- 🔄 Add comprehensive tests
  - Existing no-hardware tests cover configuration and selected detector
    contracts; full detector manager fake coverage remains incomplete
- ✅ Document the current architecture
  - See `docs/devices/detectors.rst`, `docs/setupinfo-reference.rst`, and
    `docs/design/ARCHITECTURE.md`

## Milestone 6: DAQ Safety Layer

**Goal:** Add a safety layer around all DAQ operations.

- Implement voltage limit enforcement
- Add scan-parameter validation in `SetupInfo` validators (config-level limits, e.g. galvo `maxVolt` range, dwell-time bounds)
- Implement graceful error recovery for DAQ failures
- Add logging for all DAQ operations
- Create integration tests with mock hardware

## Milestone 7: Documentation Improvements

**Goal:** Comprehensive documentation for developers, agents, and users.

- ✅ Architecture map (`docs/design/ARCHITECTURE.md` + SVG) — manager inventory, controller→manager matrix, startup flow
- ✅ Document no-hardware validation workflow
  - Complete guide at `docs/no-hardware-validation.md`
  - Command: `QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin imswitch/imcontrol/_test/unit imswitch/test_no_hardware_profile.py imswitch/test_no_hardware_ui_smoke.py -q`
  - Explains `QT_QPA_PLATFORM=offscreen` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
  - Documents test categories: `nohardware`, `ui`, `redzone`, `hardware`
  - Rules for adding future no-hardware tests without importing the full UI stack
- ✅ Document current state and known issues
  - Current baseline, active work, known limitations, and near-term priorities
    are summarized in `docs/current-state-and-known-issues.rst`
- ✅ Write developer onboarding guide
  - Practical guide at `docs/developer-onboarding.rst`
  - Environment setup, validation commands, no-hardware vs red-zone work
  - Pre-commit checklist, PR guidelines, commit message format
  - Links to architecture, no-hardware validation, and AGENTS rules
- ✅ Document all configuration options in `SetupInfo`
  - Complete reference at `docs/setupinfo-reference.rst`
  - Covers all top-level sections: detectors, lasers, positioners, rotators, scan, nidaq, availableWidgets, widgetLayout, and additional sections
  - Includes minimal examples for each section
  - Cross-links to existing device-specific documentation
- ✅ Ship the microscope-KB building guide (schema + prompts, [ScopeAId](https://github.com/LREIN663/ScopeAId)-based) under `docs/microscope-kb/`; users build their own KB locally and feed it to an external LLM project (Claude Project / Custom GPT / etc.) — ImSwitch ships no KB content and no in-app LLM
  - Includes README, schema notes, generation/system prompts, and editable YAML
    templates for hardware, limits, procedures, troubleshooting, software
    config, and safety
- ✅ Create agent task templates for common operations
  - Reusable prompt templates and review checklist are documented in
    `docs/agent-task-templates.rst`

## Milestone 8: ImControl UI & Workflow Enhancements ✅

**Goal:** Modernize ImControl UI with persistent state, improved configuration editing, and advanced imaging workflows.

- ✅ Widget state persistence framework (`WidgetStatePersistence.py` + `docs/design/WIDGET_STATE_PERSISTENCE.md`)
  - Save/load widget controller states (laser power, scan params, detector settings) to JSON
  - UI integration: File menu actions (Ctrl+Shift+S/L) with file dialogs
  - Implemented for: Laser, Settings (detector), Scan, Positioner, Recording, Rotator controllers
- ✅ Configuration editor improvements (`utility_scripts/imswitch_config_editor.py`)
  - Widget layout JSON editor with drag-drop visual widget picker
  - COM port quoting bugfix, chip/panel UI fixes
  - macOS window positioning fix
- ✅ FLIM (Fluorescence Lifetime Imaging) support
  - `FLIMHistWidget.py` (histogram display), `FLIMHistController.py`
  - Optimized pipeline with cached scan-invariant tables
  - `SwabianTimeTaggerManager` fixes (live preview, signal duplication, init=True per frame)
- ✅ Tiling/stitching workflow
  - `TilingController.py` + `TilingWidget.py` (replaced stub implementation)
  - Spiral scan pattern (`spiral_moves` utility)
  - `StitchedImage` in-memory tile stitcher (workflows)
  - `TilingInfo` config dataclass
- ✅ Napari viewer enhancements
  - `ViewerToolManager` (ROI, line, pan tools via napari Shapes layer)
  - Zoom-stable edge widths (shape edges in screen pixels)
  - 3D/2D dimension transition handling (layer padding to ndisplay dims)
  - Manual contrast range inputs, scale bar unit set to µm
- ✅ Detector improvements
  - Hamamatsu advanced subarray property editing (`docs/HAMAMATSU_ADVANCED_PROPERTIES.md`)
  - TIS camera IndexError fix
  - APD tuple mutation fix
  - Detector pixel size float step fix
  - Frame sync after advanced property changes
- ⬜ Dynamic napari layer lifecycle (still planned)
  - Currently: all `forAcquisition` detectors get permanent layers at startup
  - Plan: create/remove layers dynamically based on active detector
  - Design doc: `docs/design/plans/dynamic-layer-lifecycle.md`
- 🔄 Widget responsiveness and range cleanup
  - Dock insertion remains direct so default and JSON-defined widget placement
    keeps the original DockArea sizing behavior
  - Scan and Laser widgets no longer force full content width as the widget
    minimum
  - Recording settings scroll internally while Snap/REC remain visible
  - Positioner per-axis controls scroll internally without changing control
    signals or movement behavior
  - Embedded napari/Image minimum-height behavior still needs a safer targeted
    fix; avoid overriding the top-level Image widget minimum size globally
  - Advanced scan center spin boxes now allow negative positions
  - Advanced Scan no longer exposes partial BeadRec center/axial controls;
    BeadRec integration remains via scan geometry only
  - Follow-up plan: `docs/design/plans/widget-usability-improvements.md`
- ✅ BeadRec 2.0 (all phases implemented)
  - Phase 1: Pure model baseline for analysis defaults, ROI clipping,
    reconstruction buffering, and physical-step-size scaling
  - Phase 2: Controller reconstruction math routed through pure model while
    preserving existing scan/detector lifecycle behavior
  - Phase 3: Broad controller/widget/master access removed from `BeadWorker`;
    worker now uses narrow callables and emits reconstructed buffers
  - Phase 4: Foci/donut center finding and donut metrics extracted into pure
    model functions with structured result objects
  - Phase 5: Widget 2.0 — responsive BeadRec sizing, wider result list,
    and status/progress feedback hooks
  - Phase 6: Typed result records and passive widget-state persistence
    (image data and active reconstruction state not restored)
  - Phase 7: Immutable acquisition configuration plus structured worker
    updates for explicit progress metadata (scan behavior preserved)
  - Remaining work: Start/Stop UX, richer worker error reporting, optional
    ROI/default persistence, controller fake tests
  - Plan: `docs/design/plans/beadrec-2.0.md`
- ✅ Event-triggered EtSTED / EtMonalisa shared base hardening
  - Shared base owns session state and cleanup contracts for both modalities
  - Interrupted binary-mask recording is cleaned up on stop/close
  - Fast-laser enable failures abort arming/resume instead of silently entering
    detecting state
  - Pipeline coordinates normalize to `(N, 2)` with invalid-shape rejection
  - Focused event-triggered no-hardware tests and full unit no-hardware suite
    are passing
  - Plans: `docs/design/plans/etsted-2-0.md`,
    `docs/design/plans/etmonalisa-2-0.md`
  - Remaining work: hardware-facing validation and richer UI/UX improvements
    belong in follow-up issues, not the shared-base hardening phase
- ✅ Keyboard shortcuts for positioner stepping
  - Ctrl+Left/Right → X, Ctrl+Up/Down → Y, Ctrl+Q/Ctrl+A → Z
  - Targets the first positioner declaring the axis; step size from the existing per-axis Step field
  - Implemented via the existing `@shortcut` decorator in `imswitch/imcontrol/view/widgets/PositionerWidget.py`
  - Documented in `docs/gui.rst`

## Milestone 9: Scanning & Galvo Modernization

**Goal:** Make galvo scanning correct-by-construction and remove the manually
tuned offsets and magic numbers, especially for fast scans.

**Background — issues found during a 2026-05-22 code review:**

- **Detector-sync defect.** `phase_delay` (galvo-lag compensation) is applied
  *detector-side* by `APDManager`/`PMTManager` (they throw `phase_delay`
  samples), but it is **never applied to `line_clock`** in
  `AdvancedScanTTLCycleDesigner.__generate_all_clocks`. The
  `SwabianTimeTaggerManager` keys its pixel markers off the physical
  `line_clock` edges, so it is uncompensated. The two detector families are
  therefore offset by `phase_delay` by construction — observed as APD vs.
  TimeTagger structures shifted by ~a line.
- **Linestep `line_clock` count bug.** `__generate_all_clocks` tiles
  `line_clock` for physical `n_steps_dx[1]` (Ny), but an Advanced scan with
  `S>1` linesteps has `Ny*S` line periods → too few edges when `S>1`.
- **Inconsistent `phase_delay` default.** `APDManager` uses
  `scanInfoDict.get('phase_delay', 0)`; `PMTManager` uses a hard key.
- **Fast-axis line-start artifacts.** `GalvoScanDesigner.__d2scan_poly` /
  `__init_positioning` / `__final_positioning` place acceleration fixed points
  `dt_fix = 1e-2 µs` apart → effectively infinite jerk → excites galvo
  resonance → ringing at the start of each line.
- **Magic numbers.** `dt_fix`, `__paddingtime_full = 100`, `clock_len = 10`,
  the hand-built 10-point `BPoly` — the trajectory is constructed
  geometrically rather than derived from physical limits, and the gap to the
  galvo's real motion is patched with the manual `phase_delay` fudge factor.

**Plan:**

- 🔄 **Jerk-limited raster (minimal fix — in progress).** Replace the magic
  `dt_fix` with a finite jerk-transition time derived from a configurable
  `jerk_max` physical limit. Opt-in / config-gated, default preserves current
  behaviour. Pure trajectory math — unit-testable. Hardware verification
  required before enabling. NOTE: `test_galvo_jerk_limit.py` is currently
  skipped — it was written against a non-existent `ScanManagerBase` API
  (`signalDictTwoScan`) and needs rewriting against `makeFullScan`.
- ⬜ **Detector-sync fix.** Single source of truth for the scan-start offset:
  apply `phase_delay` when generating `line_clock`/frame clocks in the TTL
  designer; APD/PMT then stop throwing it. All detectors align by
  construction. Fix the linestep edge count (`Ny*S`) and unify the
  `phase_delay` default.
- ⬜ **GalvoScanDesigner 2.0 — feedback-based pixel binning (Option A).** Galvo
  exposes an analog position-feedback output; sample it synchronously and bin
  photons by *measured* position instead of commanded position. Removes
  `phase_delay` and the `scan_throw_*` magic numbers entirely — image geometry
  becomes correct by measurement, at any scan speed.
- ⬜ **Sinusoidal / bidirectional fast axis (Phase 2).** Once position binning
  is proven with the raster trajectory, drive the fast axis as a pure sine
  (gentlest on the mirror, real fast-scan speed) and resample from measured
  position; optionally bidirectional for 2× throughput.
- ⬜ **Auto-calibration routine.** Command a known sweep, capture position
  feedback, cross-correlate → derive lag / transfer function automatically;
  no hand-typed offsets.
- ⬜ **Magic-number cleanup.** Promote `__paddingtime_full`, `clock_len`, etc.
  to named, documented, physically meaningful config parameters.

## Milestone 10: Recording Manager Upgrade

**Goal:** Modernize `RecordingManager` and bring in the live-recording /
live-reconstruction developments from upstream ImSwitch 1.

**Background:** The upstream branch
[`ImSwitch/ImSwitch@testalab_liveRec_dev`](https://github.com/ImSwitch/ImSwitch/tree/testalab_liveRec_dev)
contains live-recording work (a ZARR streaming save path) and a larger
`imreconstruct` live-reconstruction pipeline (Zarr stream/save/load/process
workers + Gauss processor CPU/GPU + localizer/geometry models). As of
2026-05-22 that branch is still WIP — commit history is prototyping-grade,
4 files conflict with ImSwitch2's own divergence, and it carries debug noise
and a stray `loc_parms.json` artifact.

**Decision (2026-05-22):** Do **not** merge the branch now. Wait until the
upstream work is in a finished state, then **port it deliberately** — re-introduce
the intended functionality as clean, reviewed commits rather than merging WIP
history. Drop debug logging, whitespace churn, and stray artifacts on the way
in. The GPU path (`GaussProcessorGPU`) should be an optional extra.

**Plan (deferred until upstream is ready):**

- ⬜ Port the recording-side ZARR streaming save into `RecordingManager`.
- ⬜ Port the `imreconstruct` live-reconstruction pipeline as a separate phase;
  rename the `karl_*` packages to descriptive names; gate GPU behind an extra.
- ⬜ Build further recording-manager improvements from that foundation.

## Milestone 11: Scriptable WFS Workflow Ports 🔄

**Goal:** Port WidefieldStarss workflow logic into no-hardware-testable
ImSwitch model workflows that can be driven from the Console/Scripting module.

- ✅ Create the WFS workflow porting plan
  - Plan: `docs/design/plans/wfs-workflows-port.md`
  - Defines facade-first design, model-only workflow constraints, phasing,
    and parallel-agent task split
- ✅ Add headless microscope facade baseline
  - `MicroscopeFacade` and sub-facades expose the WFS-shaped API over ImSwitch
    managers
  - `MockMicroscopeFacade` supports no-hardware workflow tests and call
    assertions
  - Covered by `test_microscope_facade.py`
- ✅ Port first single-device workflows
  - `RecordingWorkflow`
  - `ZStackWorkflow`
  - `CWSTARSSWorkflow`
  - `CalibrationWorkflow`
  - Covered by workflow-specific no-hardware tests
- ⬜ Port composite workflows
  - `TilingWorkflow`
  - `DefocusScanWorkflow`
  - `SerialCWSTARSSWorkflow`
  - `MultiWellTilingWorkflow`
- ⬜ Add scripting examples and cookbook docs
  - Example scripts should live under
    `imswitch/_data/user_defaults/scripts/wfs/`
  - Add a short scripting-cookbook page after the composite workflow API is
    stable

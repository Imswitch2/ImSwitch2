# ImSwitch2 Migration Roadmap

This roadmap tracks the major milestones for the ImSwitch2 migration.
[Achieved milestones](#achieved-milestones) are summarized at the bottom
with links to the artefacts; the sections below are the active workstreams.

---

## Milestone 6: DAQ Safety Layer

**Goal:** Add a safety layer around all DAQ operations.

- ⬜ Implement voltage limit enforcement
- ⬜ Add scan-parameter validation in `SetupInfo` validators (config-level
  limits, e.g. galvo `maxVolt` range, dwell-time bounds)
- ⬜ Implement graceful error recovery for DAQ failures
- ⬜ Add structured logging for all DAQ operations
- ⬜ Create integration tests with mock hardware

---

## Milestone 9: Scanning & Galvo Modernization 🔄

**Goal:** Make galvo scanning correct-by-construction and remove the
manually tuned offsets and magic numbers, especially for fast scans.

**Background — issues found during a 2026-05-22 code review:**

- **Detector-sync defect.** `phase_delay` (galvo-lag compensation) is
  applied *detector-side* by `APDManager`/`PMTManager` (they throw
  `phase_delay` samples), but it is **never applied to `line_clock`** in
  `AdvancedScanTTLCycleDesigner.__generate_all_clocks`. The
  `SwabianTimeTaggerManager` keys its pixel markers off the physical
  `line_clock` edges, so it is uncompensated. The two detector families
  are therefore offset by `phase_delay` by construction — observed as
  APD vs. TimeTagger structures shifted by ~a line.
- **Linestep `line_clock` count bug.** `__generate_all_clocks` tiles
  `line_clock` for physical `n_steps_dx[1]` (Ny), but an Advanced scan
  with `S>1` linesteps has `Ny*S` line periods → too few edges when
  `S>1`.
- **Inconsistent `phase_delay` default.** `APDManager` uses
  `scanInfoDict.get('phase_delay', 0)`; `PMTManager` uses a hard key.
- **Fast-axis line-start artifacts.** `GalvoScanDesigner.__d2scan_poly` /
  `__init_positioning` / `__final_positioning` place acceleration fixed
  points `dt_fix = 1e-2 µs` apart → effectively infinite jerk → excites
  galvo resonance → ringing at the start of each line.
- **Magic numbers.** `dt_fix`, `__paddingtime_full = 100`, `clock_len =
  10`, the hand-built 10-point `BPoly` — the trajectory is constructed
  geometrically rather than derived from physical limits, and the gap to
  the galvo's real motion is patched with the manual `phase_delay` fudge
  factor.

**Plan:**

- 🔄 **Jerk-limited raster (minimal fix — in progress).** Replace the
  magic `dt_fix` with a finite jerk-transition time derived from a
  configurable `jerk_max` physical limit. Opt-in / config-gated, default
  preserves current behaviour. Pure trajectory math — unit-testable.
  Hardware verification required before enabling. NOTE:
  `test_galvo_jerk_limit.py` is currently skipped — it was written
  against a non-existent `ScanManagerBase` API (`signalDictTwoScan`) and
  needs rewriting against `makeFullScan`.
- ⬜ **Detector-sync fix.** Single source of truth for the scan-start
  offset: apply `phase_delay` when generating `line_clock`/frame clocks
  in the TTL designer; APD/PMT then stop throwing it. All detectors
  align by construction. Fix the linestep edge count (`Ny*S`) and unify
  the `phase_delay` default.
- ⬜ **GalvoScanDesigner 2.0 — feedback-based pixel binning (Option A).**
  Galvo exposes an analog position-feedback output; sample it
  synchronously and bin photons by *measured* position instead of
  commanded position. Removes `phase_delay` and the `scan_throw_*` magic
  numbers entirely — image geometry becomes correct by measurement, at
  any scan speed.
- ⬜ **Sinusoidal / bidirectional fast axis (Phase 2).** Once position
  binning is proven with the raster trajectory, drive the fast axis as a
  pure sine (gentlest on the mirror, real fast-scan speed) and resample
  from measured position; optionally bidirectional for 2× throughput.
- ⬜ **Auto-calibration routine.** Command a known sweep, capture
  position feedback, cross-correlate → derive lag / transfer function
  automatically; no hand-typed offsets.
- ⬜ **Magic-number cleanup.** Promote `__paddingtime_full`, `clock_len`,
  etc. to named, documented, physically meaningful config parameters.

---

## Milestone 10: Recording Manager Upgrade

**Goal:** Modernize `RecordingManager` and bring in the live-recording /
live-reconstruction developments from upstream ImSwitch 1.

**Background:** The upstream branch
[`ImSwitch/ImSwitch@testalab_liveRec_dev`](https://github.com/ImSwitch/ImSwitch/tree/testalab_liveRec_dev)
contains live-recording work (a ZARR streaming save path) and a larger
`imreconstruct` live-reconstruction pipeline (Zarr stream/save/load/process
workers + Gauss processor CPU/GPU + localizer/geometry models). As of
2026-05-22 that branch is still WIP — commit history is prototyping-grade,
4 files conflict with ImSwitch2's own divergence, and it carries debug
noise and a stray `loc_parms.json` artifact.

**Decision (2026-05-22):** Do **not** merge the branch now. Wait until
the upstream work is in a finished state, then **port it deliberately** —
re-introduce the intended functionality as clean, reviewed commits
rather than merging WIP history. Drop debug logging, whitespace churn,
and stray artifacts on the way in. The GPU path (`GaussProcessorGPU`)
should be an optional extra.

**Plan (deferred until upstream is ready):**

- ⬜ Port the recording-side ZARR streaming save into `RecordingManager`.
- ⬜ Port the `imreconstruct` live-reconstruction pipeline as a separate
  phase; rename the `karl_*` packages to descriptive names; gate GPU
  behind an extra. Coordinates with Milestone 12.
- ⬜ Build further recording-manager improvements from that foundation.

---

## Milestone 12: ImProcess (post-processing module)

**Goal:** Take ImSwitch's post-processing module (`imswitch/imreconstruct`,
to be renamed `imswitch/improcess`) from a MoNaLISA-only viewer/reconstructor
to a modality-agnostic post-processing app that handles every acquisition the
rest of ImSwitch can produce. The rename underlines that this is generalized
post-processing, not just reconstruction.

**Audit status:** ✅ complete (2026-05-29) — see
[docs/design/plans/imreconstruct-2-0.md](docs/design/plans/imreconstruct-2-0.md)
for the unified design and per-layer audit appendices.

**Background:** Today `imreconstruct` is hard-wired around MoNaLISA-style
reconstruction:

- `model/PatternFinder.py`, `model/SignalExtractor.py`, `model/ReconObj.py`
  encode SIM-pattern recovery and the MoNaLISA acquisition layout.
- `controller/ScanParamsController.py` and `view/ScanParamsDialog.py`
  expose scan-pattern parameters specific to that pipeline.
- The U-Net / U-Net-RCAN denoiser entry points (`model/UNet.py`,
  `model/UNetRCAN.py`) are MoNaLISA-trained.

Generic infrastructure already exists alongside the MoNaLISA code:
`DataObj`, `MultiDataFrame`, `WatcherFrame`, the main view shell, and the
data-edit pipeline are not modality-specific and can be reused as the
backbone of the generalized app. We also have STED / FLIM / confocal /
WidefieldSTARSS / lightsheet acquisitions for which `imreconstruct`
currently cannot do anything useful.

**Surface-level plan (to be refined):**

- ✅ **Audit + classification pass.** For every file in
  `imswitch/imreconstruct/`, tag it as `generic`, `monalisa-specific`,
  or `mixed`. Captured in
  [docs/design/plans/imreconstruct-2-0.md](docs/design/plans/imreconstruct-2-0.md)
  plus per-layer audits (`.model.md`, `.controller.md`, `.view.md`).
- ⬜ **Define a `Reconstructor` plugin interface.** Narrow contract:
  ingest a typed acquisition + config, return one or more reconstructed
  arrays + metadata. Modality picks its implementation via a registry,
  not via hard-coded paths in the main controller.
- ⬜ **Move MoNaLISA reconstruction behind the new interface.** First
  client of the registry; everything that was previously assumed-default
  becomes one entry in `reconstructors/monalisa/`.
- ⬜ **Add a "view-only" reconstructor.** Default for modalities that
  don't need reconstruction (STED, FLIM, confocal, widefield) so loading
  any ImSwitch dataset in `imreconstruct` at least shows the raw frames
  with the same data-edit / multi-data / scan-params tooling.
- ⬜ **Per-modality reconstructors.** Surface-level targets — flesh out
  with owners later:
  - STED / confocal: frame-averaging, drift correction, lifetime overlay
    when FLIM data is present.
  - WidefieldSTARSS: polarization-channel demux + per-cell metrics
    derived from the tiling workflow output.
  - Lightsheet (SNOUTY): deskew / deconvolution hooks.
  - SIM / MoNaLISA: existing pipeline as one registered reconstructor.
- ⬜ **Hook into M10's live pipeline.** Once the Zarr streaming
  reconstruction lands (Milestone 10), let it drive any registered
  reconstructor — not only the MoNaLISA path.
- ⬜ **Update docs.** Add an `imreconstruct` user/developer guide; today
  there is no dedicated page beyond the main `gui.rst` mention.

---

## Final Milestone 13: Real-World Setup Validation

**Goal:** Validate ImSwitch2 end-to-end against the five physical setups
in active use. Per-setup checklist covering the features each rig
actually uses, with both manual acceptance and (where feasible)
hardware-in-the-loop CI.

**Why this is the golden milestone:** all the framework work above
exists to be used on these instruments. A green checklist here means
ImSwitch2 has reached operational parity with ImSwitch 1 in real labs.

**Cross-cutting deliverables (per setup):**

- ⬜ A reviewed `imcontrol_setups/*.json` for the setup, validated by the
  new `imswitch_config_editor.py` cross-reference checker (Milestone 8
  follow-up) with no errors/warnings.
- ⬜ A short "first-light" checklist: boot, live view per detector,
  laser power sweep, save, reload widget state.
- ⬜ A feature-coverage checklist for that setup's distinguishing
  capabilities (sub-bullets below).
- ⬜ Sign-off log under `docs/setup-validation/<setup>.md` recording
  ImSwitch2 commit + date + tester for each green checkmark.

### 13.A — etSTED setup

Features in active use: event-triggered acquisitions, confocal
scanning, STED, FLIM (TimeTagger + phasor/exp1/moment), tiling,
rotators, camera + APD + PMT detectors, one SLM.

- ⬜ Confocal scan: image geometry verified (links to M9 detector-sync
  fix).
- ⬜ STED arm/disarm via SLM pattern switching.
- ⬜ FLIM acquisition: τ from each fit method on a reference dye
  converges (M8 closed the FLIM gap; this re-tests on hardware).
- ⬜ Event-triggered loop: full arm → detect → STED → resume cycle on
  a real sample.
- ⬜ Tiling: stitched overview + cell-target navigation.
- ⬜ Rotators: stand cube / polarizer rotation hooked through.
- ⬜ All three detector families (camera/APD/PMT) usable without
  restart.

### 13.B — MONALISA 1 setup

Features in active use: multicolor camera detection, advanced pulse
schemes, stage scanning, tiling.

- ⬜ Advanced pulse-scheme TTL output verified on scope vs. designed
  signal.
- ⬜ Multicolor camera channel mapping correct end-to-end (acquisition
  → save → `imreconstruct` view).
- ⬜ Stage scanning: drift, hysteresis, return-to-origin.
- ⬜ Tiling with multicolor stitching.

### 13.C — MONALISA 2 setup

Features in active use: 2 SLMs driving 3 lasers, camera detection, 3D
acquisition schemes, bead scanning, XYZ stage scanning, time-lapse.

- ⬜ Dual-SLM `slms` block from setup JSON loads and renders correct
  patterns per laser.
- ⬜ BeadRec 2.0 pipeline against real beads (Phase 1–7 already landed —
  this is the hardware revalidation).
- ⬜ XYZ stage scanning trajectories: bounds + step accuracy.
- ⬜ Time-lapse acquisitions: stability + scheduled cadence over
  ≥ 1 h.
- ⬜ MoNaLISA reconstruction in the generalized `imreconstruct`
  (Milestone 12).

### 13.D — WidefieldSTARSS setup

Features in active use: the WFS workflow scripting path,
tiling-with-automatic-cell-detection, polarization-sensitive detection,
XYZ stage scanning.

- ⬜ Each ported WFS workflow (M11) runs end-to-end on hardware:
  `WidefieldStarss`, `ZStack`, `CWSTARSS`, `Calibration`, `Tiling`,
  `DefocusScan`, `SerialCWSTARSS`, `MultiWellTiling`.
- ⬜ Polarization channel separation + per-cell metric extraction.
- ⬜ Auto cell-detection inside the tiling workflow drives navigation
  correctly.
- ⬜ Scripted unattended runs from
  `imswitch/_data/user_defaults/scripts/wfs/`.

### 13.E — SNOUTY lightsheet setup

Features in active use: lightsheet scanning, synchronized detection via
TriggerScope, complex acquisition combinations, bead scanning,
reconstruction.

- ⬜ TriggerScope synchronization: camera + galvo + laser pulse
  alignment validated on scope.
- ⬜ Lightsheet scan modes covered by the scan/positioner abstraction
  (may require M9 work to be in place).
- ⬜ BeadRec on lightsheet data.
- ⬜ Lightsheet deskew / reconstruction lands as a Milestone 12
  reconstructor.
- ⬜ Multi-step acquisition combinations (e.g. tile × z × time × λ)
  driven from scripting without GUI babysitting.

---

# Achieved Milestones

Short summaries of completed work; cross-links to artefacts for the full
record. Remaining sub-items that were absorbed into other workstreams or
are explicitly out-of-scope are noted inline.

## Milestone 1: Stabilization ✅

Imported the existing ImSwitch codebase, set up CI (linting + tests +
build), resolved critical lint, restored no-hardware unit-test
collection, and made the full UI launch under `QT_QPA_PLATFORM=offscreen`
with `example_no_hardware.json`. Smoke test isolates
napari/vispy/matplotlib/colour. See `imswitch/test_no_hardware_profile.py`
and `imswitch/test_no_hardware_ui_smoke.py`.

## Milestone 2: Packaging Cleanup ✅

Modernized `setup.cfg` (`python_requires = >=3.10`, relaxed pins), split
hardware packages into `[hardware]` / `[full]` extras, removed the
redundant `setup.py` shim. Core install no longer requires NI-DAQ /
Lantz / napari. *Note:* `~29 bare except:` blocks and broader
package-boundary cleanup were rolled into ongoing maintenance rather
than tracked as a separate milestone — pick off opportunistically.

## Milestone 3: Code-Level Bug Fixes ✅

Renamed the duplicate `EtSTEDInfo` class (first → `EtSTEDDeviceInfo`),
renamed `SQUIDLaserManager.py` → `ESP32LEDLaserManager.py`, deleted the
redundant `setup.py` shim, removed the dead `SLMController` /
`slmManager` layer (superseded by `SLMsController` / `slmsManager`), and
moved `__test_Manager.py` into `_test/`. Driver-mock / real-interface
separation in `model/interfaces/` is now treated as a recurring cleanup
target during touched-file work, not a single milestone.

## Milestone 3b: ImControl Backbone Cleanup ✅

Added `CommunicationChannel` signal-inventory contract tests,
reorganized the channel into domain sections, marked unused
compatibility signals (`sigGridToggled`, `sigCrosshairToggled`,
`sigScanFrameFinished`, `sigClockWidefield`) as deprecated, introduced
read-only `scanEvents` / `recordingEvents` / `eventTriggeredEvents` /
`beadRecEvents` aliases, and extracted `ScanWorkflowService` /
`BeadRecWorkflowService` for multi-step coordination. Covered by
`test_workflow_services.py` and the channel contract tests. *Remaining
removal of deprecated signals* will happen during the public-API
compatibility review tied to Milestone 13.

## Milestone 4: Hardware Abstraction Cleanup ✅

Architecture manager inventory and device reference docs cover the
active manager layer; positioner docs include Kinesis shutdown/reset
behavior and raw-driver-unit scaling. Workflow services and
no-hardware workflow facades reduce broad manager and channel coupling
in the touched paths. New/touched workflow + manager helpers use typed
dataclasses and typed signatures. Cobolt 06-01 pause fallback handles
firmware that rejects `las:paus`; Jena/Kinesis setup pitfalls are
documented and contract-tested. *Older manager modules will be migrated
opportunistically* as Milestone 13 reveals friction in specific setups.

## Milestone 5: Detector Manager Refactor ✅

Detector manager dependencies are mapped in
`docs/design/ARCHITECTURE.md` + detector reference docs; the manager
contract is documented and detector-state-persistence / selected-detector
fixes landed without breaking controller APIs. No-hardware tests cover
configuration and the selected-detector contract. *A formal narrow
facade and a full detector-manager fake* remain useful follow-ups but
no longer block downstream work — promote to a focused issue if a setup
in Milestone 13 hits the limits.

## Milestone 7: Documentation Improvements ✅

- Architecture map: `docs/design/ARCHITECTURE.md` + SVG (manager
  inventory, controller→manager matrix, startup flow).
- No-hardware validation guide: `docs/no-hardware-validation.md`.
- Current state + known issues: `docs/current-state-and-known-issues.rst`.
- Developer onboarding: `docs/developer-onboarding.rst`.
- Full `SetupInfo` reference: `docs/setupinfo-reference.rst` — every
  top-level section has a structured subsection, JSON example, and a
  device cross-reference table; `slm` (singular) and `pulseStreamer` are
  marked legacy.
- Microscope-KB building guide under `docs/microscope-kb/`
  ([ScopeAId](https://github.com/LREIN663/ScopeAId)-based; users build
  their own KB locally — ImSwitch ships no KB content and no in-app
  LLM).
- Agent task templates: `docs/agent-task-templates.rst`.

## Milestone 8: ImControl UI & Workflow Enhancements ✅

Major UI / workflow modernization. Headline items:

- Widget state persistence (`WidgetStatePersistence.py` +
  `docs/design/WIDGET_STATE_PERSISTENCE.md`); Ctrl+Shift+S/L wired
  across Laser, Settings, Scan, Positioner, Recording, Rotator.
- Config editor (`utility_scripts/imswitch_config_editor.py`):
  widget-layout JSON editor, structured editor + cross-reference
  validator for all integral system sections (focusLock, autofocus,
  tiling, scan, nidaq, etSTED, microscopeStand, pyroServerInfo,
  teensyPulse, pulseStreamer-legacy), `+ Add System Section…` picker,
  `+ Add Custom Section…` for arbitrary keys.
- FLIM support: `FLIMHistWidget` + `FLIMHistController`, optimized
  pipeline, `SwabianTimeTaggerManager` fixes, IRF peak detection,
  rep-rate-aware phasor, dist / decay mode toggle, diagnostic
  rep-rate script.
- Tiling / stitching workflow with spiral scan, in-memory
  `StitchedImage`, passive cell-marker overlay + automated targeted
  workflow path.
- Napari viewer: `ViewerToolManager` (ROI / line / pan tools),
  zoom-stable edges, 3D/2D ndisplay handling, contrast inputs, µm
  scale bar.
- Detector improvements (Hamamatsu subarray properties, TIS / APD /
  pixel-size fixes, frame sync after property edits).
- BeadRec 2.0 (Phases 1–7 landed).
- Event-triggered shared base hardened (cleanup contracts, fast-laser
  arming, coordinate normalization).
- Positioner keyboard shortcuts (Ctrl+arrows / Ctrl+Y / Ctrl+A).

*Out-of-band follow-ups* — dynamic napari-layer lifecycle, the embedded
napari minimum-height fix, and richer event-triggered UX — are tracked
as standalone issues now rather than blockers on this milestone.

## Milestone 11: Scriptable WFS Workflow Ports ✅

Plan + facade-first design under
`docs/design/plans/wfs-workflows-port.md`. `MicroscopeFacade` +
`MockMicroscopeFacade` expose the WFS-shaped API over ImSwitch managers
and are covered by `test_microscope_facade.py`. Workflows ported with
no-hardware tests: `WidefieldStarss`, `ZStack`, `CWSTARSS`,
`Calibration`, plus the composite workflows `Tiling`, `DefocusScan`,
`SerialCWSTARSS`, `MultiWellTiling`. Scripting examples ship under
`imswitch/_data/user_defaults/scripts/wfs/`. A scripting-cookbook page
will land alongside Milestone 13.D's hardware revalidation.

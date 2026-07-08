# ImSwitch2 Migration Roadmap

This roadmap tracks the major milestones for the ImSwitch2 migration.
[Achieved milestones](#achieved-milestones) are summarized at the bottom
with links to the artefacts; the sections below are the active workstreams.

---

## Milestone 9: Scanning, Galvo & DAQ Modernization 🔄

*(absorbed the former Milestone 6 "DAQ Safety Layer" on 2026-07-08 — the
safety work only makes sense against the designer/manager APIs this
milestone reshapes, so it is tracked here as a sub-plan.)*

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

**DAQ safety layer (former Milestone 6):**

- ⬜ Implement voltage limit enforcement
- ⬜ Add scan-parameter validation in `SetupInfo` validators (config-level
  limits, e.g. galvo `maxVolt` range, dwell-time bounds)
- ⬜ Implement graceful error recovery for DAQ failures
- ⬜ Add structured logging for all DAQ operations
- ⬜ Create integration tests with mock hardware

---

## Milestone 10: Recording Manager Upgrade 🔄 (almost done)

**Goal:** Modernize `RecordingManager` and bring in the live-recording /
live-reconstruction developments from upstream ImSwitch 1.

**Background:** The upstream branch
[`ImSwitch/ImSwitch@testalab_liveRec_dev`](https://github.com/ImSwitch/ImSwitch/tree/testalab_liveRec_dev)
contains live-recording work (a ZARR streaming save path) and a larger
`improcess` (formerly `imreconstruct`) live-reconstruction pipeline (Zarr stream/save/load/process
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

- ✅ **Structured Zarr save format** (2026-05-29). `ZarrStorer` rewritten
  to mirror the HDF5 layout: per-detector groups containing a `data`
  array (`(T, Y, X)`, dtype preserved from frames — no more hard-coded
  `i2`) plus a `metadata/` subgroup keyed by category (`detector`,
  `lasers`, `scan`, …). Streaming uses `resize`/slice-assign instead of
  `append`, so it works on Zarr v3. `snapImagePrev` now routes through
  `ZarrStorer.snap` for parity with HDF5. Multi-detector behaviour
  fixed: per-detector mode writes one `.zarr` per detector, single-file
  mode writes one root with multiple detector groups. RAM-backed Zarr
  recording explicitly raises `NotImplementedError` until a `MemoryStore`
  policy is designed. `ImProcess.DataObj` updated in parallel to read
  the structured layout (HDF5 and Zarr) so reconstructors see the same
  shape and merged metadata regardless of file format. Six new unit
  tests under `imswitch/imcontrol/_test/unit/test_recording.py` cover
  snap layout, streaming layout, multi-detector single-file vs
  per-detector, scan-lapse grouping, and the explicit RAM
  `NotImplementedError`.
- ✅ **Clean data-flow, Phase 1** (2026-06-15). Recording data path
  hardened end-to-end — see
  [docs/recording_dataflow_plan.md](docs/recording_dataflow_plan.md) for the
  full design and deferred phases.
  - **Dtype contract:** `DetectorManager.dtype` / `bitDepth` are the single
    source of truth (cameras learn from the latest frame; APD/PMT/Swabian
    declare explicitly). Storers create datasets from the *declared* dtype and
    **warn loudly on mismatch instead of silently casting** — no more
    "first frame defines the dtype forever".
  - **No silent casts:** APD float→buffer writes are now explicit/rounded; the
    recording worker stacks frames dtype-preserving (no float64 surprise).
  - **Off-thread writer:** disk I/O + compression moved off the acquisition
    thread (`WriterThread` + bounded backpressure queue + batched multi-frame
    chunks). Compression stays the default but no longer throttles intake.
    Robust to writer death (raises instead of deadlocking the producer) and
    surfaces `openStream` failures synchronously to the caller.
  - **Sink-abort:** `RecordingManager.abortRecording()` stops the recording and
    discards the partial output (`Storer.abortStream` deletes the file/store);
    free-running detectors are cleanly abortable. Scan *source* abort
    (mid-scan nidaq/galvo stop) is designed but hardware-gated — see the plan.
- ✅ **Live reconstruction pipeline** (2026-06/07, `feat/live-reconstruction`).
  Implemented natively instead of porting the upstream WIP branch — see
  [docs/design/plans/live-reconstruction-port.md](docs/design/plans/live-reconstruction-port.md).
  Streaming reconstructor contract (`StreamingReconstructor` /
  `StreamingSession`) so *any* registered reconstructor can consume frames
  as they arrive; file-backed `LiveSource` for Zarr/HDF5 recordings
  including folder discovery and single-file `scan{N}` timelapse streams;
  completion gate + progressive lapse updates; `frames_committed` barrier
  so mid-recording streaming never reads unwritten chunks; and a
  `stream_complete` marker that tolerates finalize/reader collisions.
  The low-latency Gauss (MoNaLISA) path is the first live consumer; the
  GPU variant stays optional. **Remaining:** validation on a real rig.
  Crashed-writer stall fallback landed (configurable timeout with automatic
  disable for lapse sources that idle between timepoints).
- 🔄 **OME-standard recording formats.** TIFF/HDF5/Zarr recordings move to
  OME conventions (OME-TIFF, OME-NGFF 0.5, HDF5 + OME-XML) via a shared
  `OmeImageMeta` — implementation and tests in place
  (see [docs/recording_ome_standardization_plan.md](docs/recording_ome_standardization_plan.md));
  back-compat sweep + docs remaining.
- 🔄 Remaining recording-manager improvements: a hard RAM cap for
  `SaveMode.RAM`, MemoryStore for in-RAM Zarr, live monitoring hooks, and
  dtype-aware compression presets (see plan doc). The `ChunkBroker`
  subscription API and producer-driven detector sources are consciously
  parked — see [Deferred / parked plans](#deferred--parked-plans).

---

## Milestone 12: ImProcess (post-processing module)

**Goal:** Take ImSwitch's post-processing module (`imswitch/improcess`,
formerly `imswitch/imreconstruct`) from a MoNaLISA-only viewer/reconstructor
to a modality-agnostic post-processing app that handles every acquisition the
rest of ImSwitch can produce. The rename underlines that this is generalized
post-processing, not just reconstruction.

**Audit status:** ✅ complete (2026-05-29) — see
[docs/design/plans/imreconstruct-2-0.md](docs/design/plans/imreconstruct-2-0.md)
for the unified design and per-layer audit appendices.

**Background (pre-M12):** `improcess` was hard-wired around MoNaLISA-style
reconstruction:

- `PatternFinder`, `SignalExtractor`, and `ReconObj.coeffsToImage` encoded
  SIM-pattern recovery and the MoNaLISA acquisition layout. As of Phase B.1
  these live under `reconstructors/monalisa/`.
- `controller/ScanParamsController.py` and `view/ScanParamsDialog.py`
  expose scan-pattern parameters specific to that pipeline.
- The U-Net / U-Net-RCAN denoiser entry points (`model/UNet.py`,
  `model/UNetRCAN.py`) are MoNaLISA-trained but architecturally generic
  and remain shared infrastructure.

Generic infrastructure already exists alongside the MoNaLISA code:
`DataObj`, `MultiDataFrame`, `WatcherFrame`, the main view shell, and the
data-edit pipeline are not modality-specific and can be reused as the
backbone of the generalized app. The first modality-specific plugins now cover
WidefieldSTARSS analysis and SNOUTY deskew/projection previews; STED, FLIM and
confocal processing remain the main open modality targets.

**Surface-level plan (to be refined):**

- ✅ **Audit + classification pass.** For every file in
  `imswitch/improcess/`, tag it as `generic`, `monalisa-specific`,
  or `mixed`. Captured in
  [docs/design/plans/imreconstruct-2-0.md](docs/design/plans/imreconstruct-2-0.md)
  plus per-layer audits (`.model.md`, `.controller.md`, `.view.md`).
- ✅ **Rename module to `improcess`** + clean up legacy references
  (Phase A, 2026-05-29).
- ✅ **Define plugin contracts:** `Reconstructor`, `Processor`,
  `ProcessingResult`, `PluginRegistry`. Registry populated from
  `setup.json` `processing:` block; falls back to standalone defaults.
  (Phase B.1, 2026-05-29).
- ✅ **MoNaLISA reconstructor plugin** under `reconstructors/monalisa/`
  (PatternFinder, SignalExtractor, coeffs_to_image, MonalisaParamsWidget,
  MonalisaProcessingResult, MonalisaReconstructor).
- ✅ **View-only reconstructor** under `reconstructors/view_only/`.
- ✅ **First processors:** FFT-based drift correction
  (`processors/drift_correct/`), FRC / single-image FRC
  (`processors/frc/`), generic projections (`processors/projection/`) and
  shared threshold/connected-component/watershed segmentation
  (`processors/segmentation/`), plus 2D Gaussian PSF/bead resolution
  (`processors/psf_resolution/`) and colocalization metrics
  (`processors/colocalization/`).
- ✅ **Segmentation source of truth.** The ImProcess segmentation analysis
  helper is now the shared implementation for the ImProcess segmentation
  processor, the tiling cell-targeting segmenter and WFS Otsu segmentation.
  Tiling keeps its workflow-facing `Segmenter` wrapper and WFS keeps
  domain-specific PSF/line-PSF modes, but Otsu thresholding, morphology,
  measurement metadata, watershed labeling and target filtering now flow
  through shared helpers. The legacy WFS `generic_otsu` spelling is accepted
  only as a saved-parameter alias for `otsu`; the UI exposes one Otsu mode.
- ✅ **Drag-and-drop ingest** on the main window for HDF5/Zarr/TIFF.
- ✅ **Standalone launch** (`python -m imswitch.improcess`) without a
  SetupInfo.
- ✅ **Generic analysis panels.** Optional graph, profile, projection, FRC,
  segmentation, PSF resolution, colocalization, ROI manager and ROI statistics
  panels are available through the `processing:` config block.
- 🔄 **Flip controllers onto the registry (Phase B.2 — pending).**
  Plugin code is in place but `ImProcessMainViewController` and
  `ReconstructionViewController` still use the legacy direct-call path.
  Verification needs Windows + `GPU_acc_recon.dll`; lands when that
  setup is available.
- 🔄 **Per-modality reconstructors.** Surface-level targets — flesh out
  with owners later:
  - STED / confocal: frame-averaging, drift correction, lifetime overlay
    when FLIM data is present.
  - ✅ WidefieldSTARSS first slice: H/V TIFF pairing, polarization-channel
    demux, anisotropy maps, per-region metrics, graph payloads and HDF5/TIFF
    save. Batch folder mode, table UI and shared Otsu segmentation are in
    place; richer layer display remains pending.
  - ✅ Lightsheet (SNOUTY): deskew and projection-preview reconstructors are
    implemented; deconvolution and real setup validation remain pending.
  - SIM / MoNaLISA: existing pipeline as one registered reconstructor.
- ✅ **Hooked into M10's live pipeline** (2026-06/07). The streaming
  session drives any registered reconstructor that implements the
  `StreamingReconstructor` contract — not only the MoNaLISA path. Live
  results update progressively in the reconstruction viewer
  (`sigLiveResultUpdated`), including mid-recording streams.
- ✅ **Results-table + plotting infrastructure.** Table-backed results
  render in the shared `ResultsTableWidget`; generic plotting (histogram,
  line, 2D histogram, PCA, optional UMAP) reuses `GraphWidget` for any
  tabular result. Foundation for the M15 localization tables and future
  filter-cutoff selection.
- 🔄 **Viewer UX: previews + one source of truth** (in progress
  2026-07-08). Live preview overlays for parameter tuning (SMLM detection
  preview on the raw-frame viewer, segmentation mask preview) and
  synchronizing the recon-list selection with the napari layer selection
  so tools and the viewer agree on the "current" image.
- ✅ **Update docs.** `docs/improcess.rst` covers launch modes,
  plugin architecture, config schema, drag-and-drop, built-in plugins,
  optional analysis panels, WFS pairing and how to write a new plugin.
  Linked from the index toctree.
- ✅ **Example minimal setup.**
  `imswitch/_data/user_defaults/imcontrol_setups/monalisa_processor.json`
  — a processing-only config that launches ImProcess with the MoNaLISA
  reconstructor + view-only fallback + drift-correct processor and no
  hardware devices declared. Recipe documented in `docs/improcess.rst`.
- ✅ **Cleanup side-quests.** Extracted shared U-Net helpers to
  `model/unet_layers.py` (~170 LOC deduplicated); fixed
  `PatternFinder.findBestPeak` arithmetic bug; added the first
  ImProcess test under `imswitch/improcess/_test/`.

---

## Milestone 14: Device Plugin Architecture 🔄 (active workstream)

**Status (2026-07-08):** the framework phases are complete; current focus
is continuing the Phase 8 gradual extraction of in-tree devices into
plugin packages, followed by publishing.

**Goal:** Let device support live in external plugin packages so the core
repository stays stable while device support evolves independently — a
napari-style model where plugins advertise device managers through package
metadata and ImSwitch imports manager code only when a configured device needs
it. Existing setup files keep working unchanged.

**Design:** [docs/design/DEVICE_PLUGINS.md](docs/design/DEVICE_PLUGINS.md);
user guide [docs/devices/plugins.rst](docs/devices/plugins.rst). GPLv3-or-later
with no proprietary in-process exception; JSON manifests (no new core
dependency).

**Plan:**

- ✅ **Public API + registry + discovery.** `imswitch.pluginapi` (the stable
  surface for plugin authors), `DeviceManagerContribution`, JSON manifest
  parsing, `imswitch.manifest` entry-point discovery, and
  `DevicePluginRegistry` with an explicit built-in table.
- ✅ **MultiManager integration.** Setup `managerName` resolves through the
  registry first, then the legacy internal import path, so existing setups boot
  unchanged; an unresolved registry-backed kind raises an actionable diagnostic.
- ✅ **Diagnostics + validation.** `python -m imswitch.imcontrol.model.plugins
  list | inspect | validate-setup`, plus best-effort managerProperties
  JSON-Schema validation.
- ✅ **Plugin template.** `imswitch-plugin-template` (private repo under the
  Imswitch2 org) — a hardware-free demo detector + laser with manifest, schema,
  setup templates, tests and CI.
- ✅ **First plugin (new device).**
  `examples/plugins/imswitch-zhinst-devices` — a Zurich Instruments lock-in
  detector with a mock mode and managerProperties schema.
- ✅ **Extraction safety net.** Known/extracted manager names map to an install
  hint, so a setup naming a manager that moved to a plugin gets a clear
  "pip install <package>" message instead of an opaque import error. Extraction
  checklist in `docs/devices/plugins.rst`.
- 🔄 **Gradual extraction of in-tree devices (Phase 8) — next up.** First
  extraction done: `examples/plugins/imswitch-device-thorlabs` moves the
  Thorlabs TSI camera into a plugin (legacy class name kept as an alias;
  in-tree copy retained until the plugin is published). Continue with the
  remaining non-safety-critical cameras and simple serial devices, one
  plugin per vendor, following the extraction checklist in
  `docs/devices/plugins.rst`; lasers/DAQ/stage paths need extra review.
- ⬜ **Publish.** Make the template a public GitHub template repo; publish
  device plugin packages to PyPI; remove in-tree copies once their plugins are
  published (the install hint then becomes live).

---

## Milestone 15: Single-Molecule Localization Microscopy (SMLM) 🔄

**Goal:** Make ImSwitch2 a first-class SMLM platform end to end — acquire a
blinking image stack, localize single emitters into a coordinate table with
properties, process that table (drift correction, grouping, filtering), and
render it in-house — while treating the external
[napari-storm](https://github.com/super-resolution/napari-storm) plugin as the
premium GPU point-cloud renderer via a clean data handoff, not a dependency.

**Detailed plan:**
[docs/design/plans/smlm-localization-port.md](docs/design/plans/smlm-localization-port.md)
— phased, first phases spelled out. **Scope for now: localization + in-house
rendering only.** COMET drift correction (GPU-optional), grouping, and
advanced filtering are named as future phases but deliberately out of the
initial scope.

**Why this fits ImProcess (M12) rather than being a rewrite:**

- **Acquisition (ImControl) is already there** — camera + laser(s) + optional
  filter flipper cover a STORM/PALM/DNA-PAINT rig with no new hardware work.
- **The localization *compute* plugs into the existing reconstructor
  registry.** A `Localizer` reconstructor implementing the
  `StreamingReconstructor`/`StreamingSession` contract localizes frames as
  they arrive — the streaming/`LiveSource`/`frames_committed` machinery from
  Milestone 10 is directly reusable, so live localization during acquisition
  comes almost for free.
- **The one true gap is the *result type*.** `ProcessingResult` today is
  image/array-centric (N-d array → napari image layer). SMLM's native output
  is a **coordinate table** (`frame, x/y/z, sigma_x/y/z, photons`). M15 adds a
  `LocalizationResult` around that recarray; the existing results-table and
  processor-chain infrastructure then hosts filter/group/drift as table
  processors.
- **In-house 3D rendering reuses the image viewer, no points layer needed.**
  Following [pyMINFLUX](https://github.com/bsse-scf/pyMINFLUX)'s approach,
  "render" is a pure-numpy step that turns the coordinate table back into an
  image — 2D/3D **histogram binning** or **fixed-Gaussian splatting**. The
  rendered volume is a normal image `ProcessingResult` the embedded napari
  viewer already displays (3D via the dims slider). napari-storm stays the
  separate, cutting-edge GPU particle renderer, fed the same recarray.

**Surface-level plan (refined in the plan doc):**

- ✅ **`LocalizationResult` + canonical schema** (2026-07). Table-backed
  `ProcessingResult` around the `(frame, x/y/z, sigma_x/y/z, photons)`
  recarray (`model/localization_schema.py` / `localization_result.py`),
  with pixel-size/units metadata, a lazy histogram preview as the default
  viewable data, results-table/plot projections, and CSV/HDF5 persistence.
- 🔄 **`Localizer` reconstructor** (batch ✅, streaming ⬜). Picasso-style
  net-gradient `detect_spots` + centroid/MLE `fit_spots` as a
  pure-function core behind `reconstructors/smlm/SmlmLocalizer`; the
  `StreamingSession` that localizes per chunk (live SMLM via the M10
  stack) is still open. A live detection-preview overlay for tuning the
  threshold on raw frames is in progress (2026-07-08).
- ✅ **In-house renderer** (2026-07). Pure-numpy histogram +
  fixed-Gaussian rendering (`analysis/smlm_render.py`) exposed as the
  `processors/smlm_render` processor producing an image
  `ProcessingResult` the existing viewer displays.
- ✅ **napari-storm handoff** (2026-07). `analysis/smlm_export.py` writes
  the Picasso-format HDF5 that napari-storm's reader consumes
  (`LocalizationResult.save(fmt="picasso")`), so the premium renderer is
  one export away without a runtime dependency.
- ⬜ **Future phases (out of initial scope):** COMET drift correction
  (GPU-optional), grouping/linking, advanced filtering, 3D (astigmatism/PSF)
  fitting, throughput-oriented (vectorized/GPU) localization.

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
  → save → `improcess` view).
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
- ⬜ MoNaLISA reconstruction in the generalized `improcess`
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
  correctly, using the same shared segmentation kernel as ImProcess and WFS.
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
- Time-resolved detector workflows: a generic time-resolved detector
  contract (`model/timeresolved`) plus opt-in workflows
  (`model/workflows/time_resolved.py`) for binned photon-arrival cubes,
  software gated-STED and tau-STED, with `SwabianTimeTaggerManager` as the
  first backend via
  `api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=...)`.
  Mock facade + unit tests, example scripts under `scripts/timeresolved/`,
  and docs
  ([plan](docs/design/plans/time-resolved-detector-workflows.md),
  `docs/scripting-time-resolved-workflows.rst`). Software foundation
  complete; hardware validation pending.
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

---

## Deferred / parked plans

Designs that were evaluated and consciously **deferred** — not abandoned.
Captured here so the analysis isn't repeated from scratch.

### ChunkBroker / producer-driven acquisition (deferred 2026-06-23)

The `ChunkBroker` subscription API and the producer-driven acquisition
rewrite (Recording data-flow Phases 1.5 / 2 / 3) are **designed but
deferred**. Full design + effort/risk breakdown:
[docs/recording_dataflow_plan.md](docs/recording_dataflow_plan.md).

**Why we looked at it:** it is the prerequisite for a file-less, lowest-latency
live-reconstruction source (`ChunkBrokerLiveSource`, the "P7" item in
[docs/design/plans/live-reconstruction-port.md](docs/design/plans/live-reconstruction-port.md)),
and for removing the acquisition-loop `time.sleep` / raising max throughput.

**Why we deferred it (2026-06-23 review):**
- **Live reconstruction does not need it.** The file-based live path
  (Zarr/HDF5, single-file `scan{N}` timelapse, folder discovery) already works.
  A broker-backed live source would initially publish through the existing
  polling path, so it gives a file-less plumbing path but **not** lower latency
  or higher throughput until the full producer-driven migration (Phase 2) lands.
- **The real standalone value is multi-consumer robustness, not speed:** explicit
  per-subscriber queues + drop accounting (today a slow registered consumer
  silently loses its oldest frames, `DetectorManager.MAX_QUEUED_CONSUMER_FRAMES`),
  recording isolation from slow consumers, and a tested/introspectable fan-out
  contract. Worth doing **only if** running recording + BeadRec + workflow +
  live-view simultaneously becomes a real correctness pain — currently it does
  not justify the cost on its own.
- **The throughput/no-`sleep` win lives in Phase 2**, which rewrites real device
  managers (camera pull→push adapters, scan-detector push), cannot be
  CI-validated (`docs/no-hardware-validation.md`), and is rated ~1–2+ weeks /
  high risk.

**Revisit when:** there's a concrete throughput/CPU-pinning pain point or a
demonstrated multi-consumer frame-loss problem, *and* rig time is available to
validate Phase 2. If revisited, the broker shim (Phase 1.5a) is the bounded,
no-hardware, agent-friendly first step; the producer migration (Phase 2) is not
an agent task (hardware-gated concurrency).

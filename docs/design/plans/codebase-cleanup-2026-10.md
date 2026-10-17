# Codebase cleanup 2026-10 — work packages

**Branch of origin:** `chore/codebase-cleanup` (off `main` at 5a592b356).
**Status:** the trivial removals are done on that branch (see *Done in the
first pass*). Everything below is a work package for a separate agent run.

**How to run these.** One package per agent session, **one at a time** —
agent sessions share one checkout. Each package goes on its own branch off
`main` (after `chore/codebase-cleanup` has merged) and ends in one PR. Every
package names its decisions up front: an agent must not decide a
**DECISION** item itself; it stops and asks Lenny. The audit findings below
were verified against `main` at 5a592b356; re-grep before acting, since
other branches keep landing.

Test invocation (CI-equivalent, from the worktree root, with a throwaway
`HOME` because importing `imswitch` syncs `~/ImSwitchConfig`):

```bash
HOME=$(mktemp -d) PYTHONPATH=$PWD QT_QPA_PLATFORM=offscreen python -m pytest -p no:napari -q <paths>
```

---

## Done in the first pass (`chore/codebase-cleanup`)

- Root clutter deleted: `openhands_tasks/` (finished briefs), `paper/`
  (upstream 2021 JOSS sources; the DOI stays in `CITATION.cff`),
  `.env.example`, `.flake8`, `requirements.txt`, `requirements-dev.txt`,
  `main.py`, three broken example scripts (`workflow_facade_api_demo.py`,
  `widget_state_save_load_demo.py`, `widget_state_persistence_demo.py`),
  `utility_scripts/beadrec_reconstruct_offline.py` (superseded by the
  BeadRec reconstructor), `utility_scripts/templates/Test_device` (leaked
  into users' template stores through the config editor migration).
- Dead `AGENTS.md` links fixed (README, CONTRIBUTING, GOVERNANCE); imcommon
  tests no longer run in two CI lanes; `imswitch.spec` un-ignored;
  `licenses/` shipped in the sdist; the shipped `NidaqPositionerManager`
  template test reads the real path (it always skipped).
- `CommunicationChannel`: eight signals with no producer and no consumer
  removed, the two listeners on never-emitted useq stubs removed, the twelve
  unused alias groups and `DEPRECATED_SIGNALS` removed. `signals()` API
  unchanged.
- Docs: ~60 finished plans, audits, agent briefs and duplicates deleted;
  still-open plans moved under `docs/design/plans/`; references in code and
  docs to deleted notes marked "(retired; see git history)" — `git log --
  <path>` recovers any of them. `ROADMAP.md` deleted; its open items now
  sit under *Open work* in `docs/current-state-and-known-issues.rst`.
- Dead code: WellPlate widget/controller, `stitching_animation.py`,
  `runtime_result_processors.py`, `profile_helpers.py`, commented "Et"
  prototype lines, stale header in `ScanControllerAdvanced.py`;
  unreferenced legacy screenshots in `docs/images`.

---

## WP-1 Architecture page (docs) -- done

The old architecture note became `docs/architecture.rst` (Extending
ImSwitch2 toctree) and the device plugin design rationale was folded into
`docs/devices/plugins.rst` ("Design notes"); both markdown files are
deleted (`git log -- <path>` recovers them). Not carried over: the
per-device manager and widget inventories, the controller-to-manager matrix
and the Teensy wire protocol summary (the firmware repository owns it).

## WP-2 Docs folds, changelog, current state -- DONE

Commits on `chore/cleanup-docs`: `bfd7ba00c` (Hamamatsu), `c10425bf7`
(TriggerScope quirks, three unused screenshots), `ee9dad154` (state
persistence page), `53bc80d08` (acquisition layout), `bd1eb88ae` (changelog),
`29b5d37cc` (current state), `e9909fa01` (`conf.py` version). The three
markdown notes and the three acquisition-layout plan files are deleted
(`git log -- <path>` recovers them).

Deliberately left:

- The Hamamatsu note is in `gui.rst` (Settings widget) with a pointer from
  `devices/detectors.rst`. The TriggerScope quirks are a *Firmware quirks*
  subsection of the TriggerScope panels in `gui.rst`, not on
  `devices/positioners.rst`, which has no TriggerScope card. The firmware is
  not in this repository, so quirks 1, 3 and 5 rest on the old note and the
  panel docs; quirk 2 (a "not a bug" remark) was dropped, quirk 4 is verified
  against `trimRasterLengthForFirmwareBoundary`.
- The acquisition layout section in `Hdf5datafile.rst` carries the schema, the
  stored keys, validation, completeness markers and the resolver precedence.
  The plan's delivery sequence, test matrix, producer and reconstructor
  preflight sections, and the 2026 audit history were not carried over.
- `current-state-and-known-issues.rst`: every known issue still holds, except
  that the event-triggered signals are named instead of counted. The
  `test_snouty.py` CI ignore is still in `ci.yml` (the file passes locally on
  macOS; the CI failure was not reproduced). Left for the maintainer: the two
  **DECISION**s on the long-range scanning items (feedback-based pixel binning,
  sinusoidal fast axis, automatic lag calibration), and the per-rig validation
  write-up (no evidence is in the repository).
- `changelog.rst` is split at the tags by the commit that first added each
  bullet (all bullets placed; the two "A fresh install loads ImProcess..."
  bullets differ in wording and were both kept). `docs/microscope-kb/` is WP-7.

## WP-3 Retire the PointScan scan widget

Nothing shipped uses `scanWidgetType: "PointScan"`; Advanced covers it except:

- **DECIDED (Lenny, 2026-10-17): drop** the `TTL_sequence` /
  `TTL_sequence_axis` feature (`PointScanTTLCycleDesigner.py:73-200`). The
  expert scan (feat/scan-engines, not yet on main) covers it in principle.
- Port the `changed3StepDelayPar` API wrapper to `ScanControllerAdvanced`
  (the field exists; documented in `api/api.imcontrol.rst:75`, `gui.rst`).

Delete `ScanControllerPointScan.py`, `ScanWidgetPointScan.py`,
`ScanManagerPointScan.py`, `PointScanTTLCycleDesigner.py`,
`docs/images/auto/ScanWidgetPointScan.png`; update the lazy registries
(`controllers/__init__.py`, `widgets/__init__.py`, `managers/__init__.py`),
`MasterController.py` (~l.4, 91-105), `builtin_templates/sections/scan.json`
(options **and** its mismatched TTL default), `tools/screenshot_widgets.py`,
`SetupInfo.py:463` docstring, `scripts/diagnostics/test-galvoscandesigner.py`,
`docs/gui.rst`, `docs/advanced-scanning.rst`. Port or delete the tests that
import it (`test_scan_run_ownership`, `test_scan_driven_pixel_size`,
`test_scan_design_refusal`, `test_scan_lifecycle`,
`test_point_detector_single_axis`; fixtures in
`test_hamamatsu_mock_triggering`, `test_mock_scan_simulation_coordinator`,
`test_nidaq_clock_contract`) — the behaviour they pin should be pinned on
Advanced where Advanced has it.
**Keep** the alias strings in `basecontrollers.py:~391` and
`imcommon/model/WidgetStatePersistence.py:~52` so old saved states load;
a setup still naming PointScan should fail with a message pointing at
Advanced (add that in `MasterController`/`ImConMainController`).

## WP-4 Retire the MoNaLISA scan widget -- DONE (commit see git log, "Retire the MoNaLISA scan panel")

Deleted `ScanControllerMoNaLISA`, `ScanWidgetMoNaLISA`, `ScanManagerMoNaLISA`
and the screenshot; `scanWidgetType: "MoNaLISA"` now stops with an error
pointing at `Base`/`Advanced` (`retiredScanWidgetTypeError`). Auto-axial XZ/YZ
scans with bead-centre search are dropped. Also removed with it: the
`CommunicationChannel` signals `sigQueryCenterCoord`,
`sigCenterCoordPipelineFinished`, `sigUpdateBeadRecCenter`,
`sigShowBeadRecCenterCross`, `sigAutoAxialToggled`, `sigNewAxialListBuffer`,
`getNextAxial` (channel and `SuperScanController`), the whole
`BeadRecWorkflowService`/`beadRecWorkflow`, BeadRec's `autoAxial` and
centre-query/cross handlers and `BeadRecWidget.displayCenterCoord`. The two
mock setups moved to `Base` (they use the Beta designers). Legacy
`ScanControllerMoNaLISA` state-name aliases are kept.

Left on purpose: `BeadRecController._isAxialImage`/`axial_name` on result
records (still read from saved BeadRec state and covered by
`test_beadrec_ome_save`), and `find_bead_center` in the bead-recognition model
(tested, now without a caller).

## WP-5 CommunicationChannel, second pass

The first pass removed only what had zero producers and consumers. Left:

1. Replace the request/response signal pairs with direct calls on
   `ScanWorkflowService`: `sigRequestScanParameters`/`sigSendScanParameters`
   and `sigRequestScanFreq`/`sigSendScanFreq` (EtSTED runner,
   RecordingController, EventTriggeredBase).
2. The three event-triggered flags: `sigInitiateEtMonalisa` and
   `sigInitiateEtSnouty` are emitted and nobody listens. Delete them (and the
   emits) unless a listener is wanted (**DECISION** only if someone wants the
   joystick lock during ET runs that the commented connect hinted at).
3. `sigRunScanTriggerScopePLSRMulticolor` duplicates `sigRunScan` for one
   target: use `scanWorkflow.run_scan_from(...)`.
4. `sigClockWidefield` is EtSnouty's private timer on the global bus: move it
   onto the EtSnouty controller.
5. `sigScanBuilt` on the channel: every real listener uses
   `nidaqManager.sigScanBuilt`; only tests connect the channel one. Remove
   the channel copy and its two emits, or document why it stays.
6. Move the scan-source registry (`getScanSource`/`getRecordingScanSource`
   are near-duplicates), the four copy-paste BeadRec accessors
   (`getDimsScan`, `getScanStepSizes`, `getNumLineSteps`,
   `getFramesPerScanPixel`) and the focus-lock registry out of the channel
   into a resolver/service. Remove the unused `getNumLineSteps` and
   `isExecuting`.
7. Shorten the three-hop script relay (`runScript` → module
   `sigRunScript` → imscripting `sigRunScript`, and back through
   `sigExecutionFinished` → `sigScriptExecutionFinished`).
8. Bugs found by the audit (each needs a test):
   - `sigSLMMaskUpdated`: SLMController listens on the channel, but
     `MasterController` never forwards `SLMManager.sigSLMMaskUpdated` — the
     mask display is never driven through it.
   - `acquireImage` (`@APIExport`) appends to `self.output`, which only
     `runScript` creates → `AttributeError` outside a script.
   - `sigSaveFocus` is in the public `signals()` API but nothing emits it, so
     a script waiting on it blocks forever. **DECISION:** emit it from the
     focus lock, or remove it from `signals()` (scripting docs change).
9. sharedAttrs: `LSXYR_Scan:*` keys have no reader anywhere; the
   `_attrCategoryDevices` constants in 6 TriggerScope/LightSheet controllers
   are never written; `LightSheetMulticolorController.attrChanged` is never
   connected.

Keep `test_communication_channel_contract.py` and the inventory JSON in step.

## WP-6 Compatibility shims and stray tests

Repoint callers, then delete the shim:

| Shim | Callers |
|---|---|
| `imcontrol/controller/ShortcutManager.py` | `ImConMainController.py`, 6 tests |
| `imcontrol/model/bead_fits.py` | `BeadRecController.py:~750` |
| `imcontrol/model/bead_recognition.py` | `BeadRecController.py:~24`, 3 tests |
| `imcontrol/model/WidgetStatePersistence.py` | `imcontrol/model/__init__.py` |
| `imcontrol/model/state_contracts.py` | `basecontrollers.py` |
| `improcess/layer_selection.py` | ROIStats/ROIManager/Segmentation/Multicolor widgets, tests |

Keep `imcontrol/model/workflows/recording.py` (user scripts import it).
Move the tests of `tests/test_bead_fits.py` (never collected: outside
`testpaths` and every CI lane) into `imswitch/imcommon/_test/test_bead_fits.py`
importing from `imcommon.algorithms`, and delete `tests/`.

## WP-7 Packaging, CI and scripts

**DONE (2026-10-17).** Plugin test lane in `ci.yml` (all three plugins pass),
build fallback and missing `apt-get update` fixed, ruff `exclude` trimmed,
scripts consolidated (`scripts/diagnostics/`, `scripts/utilities/`,
`scripts/utilities/scopeaid/` replacing `docs/microscope-kb/`), README tree,
improcess README and `imswitch.spec` docstring corrected, Hamamatsu SLM
`.lib/.h` removed (the LabVIEW `.ini`/`.aliases` stay: the LabVIEW runtime reads them next to the DLL). `examples/plugins/` was not moved (hard-coded
paths in `external.py`). **Still open (maintainer decision):** exclude
`imswitch/_data/libs/*.dll` (~66 MB) from non-Windows wheels/sdists.

## WP-8 Open plans triage

**DONE (2026-10-17).** Maintainer ground truth: main is rig-validated, so a plan
whose code is merged and whose only remaining item is a rig check is finished.
Each plan was checked against the tree.

Deleted (implemented; git history keeps them): `acquisition-layout-contract.md`
and `acquisition-layout-rig-*.md` (WP-2), `detector-acquisition-selection.md`
(Phases 1-5 are in code - `setDetectorSelected`, GUI selection, participant gate;
the 17 code citations now point to `docs/scan-lifecycle.rst`),
`improcess-ome-read-compatibility.md` (only a conditional live-source `data`
name remains, relevant if the Zarr array is ever renamed `0`),
`improcess-workflow-editor.md` (Phase 4 was optional), `imscripting-workflow-hangs.md`,
`manager-audit-todo.md` (stale: the PulseStreamer stdout redirect is restored in
a `finally`, LeicaDMIManager is gone, the rest landed), `time-resolved-detector-workflows.md`,
`tis-camera-ic4-migration.md`, `ws-integration.md` (it never had the v4 wire
protocol section two code comments cited).

Surviving plans (all rewritten with a `Status (2026-10-17)` paragraph):

| Plan | Remains |
|---|---|
| `acquisition-metadata-channel-retirement.md` | P3 `writeLegacyScanMetadata` flag, P4 default-off/delete; release-gated - **DECISION** |
| `config-editor-discovery-and-schema.md` | CLI `validate-setup` passes no `ValidationContext` (weaker than the editor); resources/`TemplateStore` still in the view package; `x-imswitch-*` UI annotations; Phase 6 UX |
| `ci-improvement.md` | B1 marker completion, B3 UI lane, Snouty ignore, Tier C shared setup |
| `dynamic-layer-lifecycle.md` | not implemented (UX) - **DECISION** adopt or drop |
| `improcess-lazy-virtual-image-source.md` | rest of phase 2, phases 3-4 |
| `improcess-roi-manager-2-0.md` | now 111 lines: P-F2 container ids and the processing footprint (both need a central save path), Fiji fixture corpus |
| `pointscan-confocal-sted-performance.md` | O1-O5 unimplemented, need rig numbers |
| `recording-dataflow.md` | `SaveMode.RAM` hard cap, scan source abort, Phases 1.5-3 (`ChunkBroker`) - **DECISION** whether wanted |
| `scan-acquisition-order-spec.md` | Draft 4, nothing implemented - **DECISION** adopt or reject |
| `scanner-calibration-widget.md` | not implemented (P-0 first); section 10 decisions open |
| `tiled-target-timelapse-smart-events.md` | T5 UI and controller adapters (model layer exists, unused) |
| `tiling-multidetector.md` | Phase 1b session entry: `TilingController.py:~1543` reads `self._commChannel.__dict__.get('_main')`, but the channel stores a name-mangled `__main`, so it is always `None` and tiling always falls back to snapshots. Use `controllerRegistry()` **together with** explicit session ownership (the lookup alone stalls the run); `test_tile_layout_partition.py:~185` masks it with `SimpleNamespace(_main=...)`. Phase 3 (3D) deferred |

Also flagged (not in a plan): EtSTED/EtMonalisa design their slow scan with
`scanManager.makeFullScan` directly (`EtSTEDTriggeredScanRunner.py:~54`),
bypassing `ScanControllerAdvanced._make_full_scan` — with Advanced and
line steps > 1 the scan and TTL designers disagree on `n_linesteps`, and no
AO power is injected. Not verified at runtime.

## Not to remove (checked)

- Base scan widget (`ScanWidgetBase`/`ScanManagerBase` are Advanced's base
  classes; tutorials and two mock setups use it).
- EtSTED/EtMonalisa/EtSnouty, TriggerScope* widgets — not superseded.
- 46 hardware managers no shipped setup uses — picked by `managerName` in
  users' rig files; remove only with the rig owners.
- `PiezoconceptZManager2` (newer firmware), `PyCoboltManager` (still used).
- Widgets with no setup and no tests (`BFTimelapse`, `Watcher`, `FLIMHist`,
  `MotCorr`, `BSC203`) — confirm with rig owners before touching.

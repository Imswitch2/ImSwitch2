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

## WP-2 Docs folds, changelog, current state

1. Fold into rst and delete the md:
   - `docs/HAMAMATSU_ADVANCED_PROPERTIES.md` → `gui.rst` Settings section
     (Advanced Properties tab, safety rules) + one line on the Hamamatsu
     card in `devices/detectors.rst`.
   - `docs/triggerscope_firmware_quirks.md` → a *Firmware quirks* subsection
     on the TriggerScope cards (`devices/positioners.rst`); items 1, 3, 5
     are still live, re-verify 2 and 4.
   - `docs/state_persistence_contract_spec.md` → new
     `docs/state-persistence.rst` under *Extending ImSwitch2* (the
     `StatefulComponentMixin` contract, hardware-active exclusions).
   - The contract tables of `docs/design/plans/acquisition-layout-contract.md`
     → `scan-lifecycle.rst` or `Hdf5datafile.rst` (layout metadata keys
     readers rely on); then delete the plan once the rig pass of
     `acquisition-layout-rig-validation.md` is done.
2. `changelog.rst`: everything since v0.1.0 sits under *Unreleased*. Split it
   at the `v0.2.0` and `v0.3.0` tags (`git log v0.2.0..v0.3.0`). Label the
   ImSwitch 1.x section "Upstream history".
3. `current-state-and-known-issues.rst` now carries the roadmap's open items
   (*Open work*). Re-verify each known issue there against the code, and
   settle two **DECISION**s left from the roadmap: keep or drop the long-range
   scanning items (feedback-based pixel binning, sinusoidal fast axis,
   automatic lag calibration). Per-rig setup validation is DONE (all rigs,
   2026; every recent hardware PR was rig-tested) but undocumented — a short
   write-up page is welcome if the evidence exists, never invented.
4. Screenshots: add `.. image::` for `auto/SLMsWidget.png`,
   `auto/TriggerScopeRasterWidget.png`,
   `auto/TriggerScopePLSRMulticolorWidget.png` in `gui.rst`.
5. `docs/microscope-kb/` is ScopeAId tooling data, not docs: move it next to
   `utility_scripts/scopeaid_seed_from_config.py` (update its path at ~l.620).
6. `docs/conf.py`: read `version`/`release` from `imswitch.__version__`.

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

## WP-4 Retire the MoNaLISA scan widget

Not the ImProcess MoNaLISA reconstruction — that stays (including
`reconstructors/monalisa/legacy.py`, which is live). Only the imcontrol scan
panel: `ScanControllerMoNaLISA.py`, `ScanWidgetMoNaLISA.py`,
`ScanManagerMoNaLISA.py` (a duplicate of `ScanManagerBase`).

- **DECIDED (Lenny, 2026-10-17): drop** auto-axial XZ/YZ follow-up scans with
  bead-centre search (`ScanControllerMoNaLISA.py:197-366`); the expert scan
  covers it in principle. Also remove the plumbing that only it uses: BeadRec
  `autoAxial`/`axialListBuffer`/`getNextAxial`, `WorkflowServices.py:~777-811`,
  channel `sigAutoAxialToggled`/`sigNewAxialListBuffer`/`getNextAxial`.
- *Cont. Laser Pulses* and TTL start/end in ms exist on Base, which stays.
- Repoint `mock_scan_setup.json` and `mock_scan_monalisa_live.json` to
  Advanced (or Base), update `_data/user_defaults_history.json`
  (`tools/update_user_defaults_history.py`; `test_user_defaults_sync` checks
  it; its `__comment__` also still cites the retired
  `live-reconstruction-port.md`), and port the tests listed in the audit (`test_scan_run_ownership`,
  `test_scan_design_refusal`, `test_scan_position_reference`,
  `test_scan_component_state`, `test_scan_lifecycle`, ...).
- Same alias/compat rule as WP-3.

Do WP-3 before WP-4: they touch the same registries and tests.

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

1. CI lane for `examples/plugins/*/tests` (8 test files, three plugins users
   are told to `pip install`). Consider moving `examples/plugins/` to a
   top-level `plugins/` — they are real packages, not examples
   (`imcontrol/model/plugins/external.py` hard-codes the `source=` paths).
2. `imswitch/_data/libs/*.dll` (~66 MB, Windows-only MoNaLISA GPU path) ship
   in every wheel and sdist on every platform. **DECISION:** exclude from
   non-Windows wheels / make a separate download.
3. `imcontrol/model/interfaces/hpkSLMdaLV_cdecl_64bit/`: the `.lib`, `.h`,
   LabVIEW `.aliases`/`.ini` are not needed at runtime.
4. `ci.yml`: drop the `python -m build --no-isolation 2>/dev/null || ...`
   fallback that hides errors; `imswitch-test.yml` runs `apt install` without
   `apt update`.
5. `pyproject.toml`: the ruff `exclude` names three `scripts/diagnostics`
   files by bare name and CI only lints `imswitch/` — drop them.
6. One home per kind of script: `tools/` (dev generators, keep),
   `scripts/diagnostics/` (hardware probes: move `utility_scripts/bsc203_diag.py`
   and `diagnose_tis_camera.py` there; update `docs/devices/positioners.rst`,
   `docs/TISCamera.rst`, the comment in `BSC203StageManager.py`), and
   `utility_scripts/` → `scripts/utilities/` for operator tools (keep the
   `conftest.py` path for `aa_aotf_calibration.py` working). Fix both
   READMEs (each lists a subset). Note the config editor's legacy-template
   migration reads `utility_scripts/templates`; keep that path string.
7. README project tree: add `scripts/`, `examples/`, `release/`, `tests/`
   (or what remains after WP-6).
8. `imswitch/improcess/README.md` links `IMPROCESS_REVIEW.md` and
   `controller/WatcherFrameController.py`, neither exists.
9. `imswitch.spec` docstring recommends `pip install -e .`; CI says an
   editable install breaks the bundle. Align.

## WP-8 Open plans triage

After WP-1..7, each file left in `docs/design/plans/` must either have open
work with an owner or go. Current status (2026-10-17):

| Plan | Remaining |
|---|---|
| `acquisition-layout-contract.md`, `acquisition-layout-rig-*.md` | rig pass only (fold tables per WP-2) |
| `acquisition-metadata-channel-retirement.md` | P3 (stop writing category-A `ScanStage:*`/`ScanTTL:*` behind a switch), P4 |
| `config-editor-discovery-and-schema.md` | Phase 4 CLI, Phase 5 partial, Phase 6 — re-verify, lots landed since |
| `detector-acquisition-selection.md` | Phase 5 behind a hardware smoke gate; 17 code citations |
| `dynamic-layer-lifecycle.md` | not implemented |
| `improcess-roi-manager-2-0.md` | P-F2, Tracks C/D; 2755 lines — trim to the open tracks |
| `improcess-workflow-editor.md` | Phase 4 |
| `improcess-lazy-virtual-image-source.md` | Phase 2 partial, Phase 3 |
| `improcess-ome-read-compatibility.md` | lapse/live sources hard-code the `data` array name |
| `imscripting-workflow-hangs.md` | rig P-8 only |
| `manager-audit-todo.md` | mostly stale; PulseStreamer global `sys.stdout` redirect still open |
| `pointscan-confocal-sted-performance.md` | O1–O3, rig-gated |
| `recording-dataflow.md` | Phases 1.5/2/3 deferred |
| `scan-acquisition-order-spec.md` | Draft 4, nothing implemented — **DECISION** adopt or reject |
| `scanner-calibration-widget.md` | P-0 not implemented |
| `tiled-target-timelapse-smart-events.md` | T5 |
| `tiling-multidetector.md` | the per-tile session: `TilingController.py:~1543` reads `self._commChannel.__dict__.get('_main')`, but the channel stores a name-mangled `__main`, so it is always `None` and tiling always falls back to snapshots. Use `controllerRegistry()` **together with** explicit session ownership, as the plan says (the lookup alone stalls the run). `test_tile_layout_partition.py:~185` masks it with `SimpleNamespace(_main=...)`. |
| `time-resolved-detector-workflows.md` | Swabian rig, >3D output, second backend |
| `tis-camera-ic4-migration.md` | external-TTL Phase 0, rig |
| `ws-integration.md` | hardware verification |
| `ci-improvement.md` | B3 (UI lane), Tier C |

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

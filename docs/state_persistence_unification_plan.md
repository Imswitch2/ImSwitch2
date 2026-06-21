# State persistence & setup-modes unification — audit and plan

> **STATUS: COMPLETE (2026-06-18).** All phases implemented and merged into
> `codex/testalab-scandev-port` (see `docs/agent_tasks/phase*.md` for the
> per-phase task specs). Every state-bearing controller now implements the single
> `StatefulComponentMixin` contract; global startup persistence and named setup
> modes are the two consumers. The legacy `getWidgetState`/`setWidgetState`,
> `getSetupModeState`/`applySetupModeState`, and `SetupModeMixin` interfaces have
> been removed. Deferred by decision: legacy-preset *storage* retirement (Laser
> `setupInfo.laserPresets`, SLM `saveParams`) — those remain as separate
> convenience features. Full unit suite: 632 passed, 4 skipped.

Audit targets:
- `imswitch/imcontrol/model/WidgetStatePersistence.py` (global state persistence)
- `imswitch/imcontrol/controller/SetupModeController.py` + `SetupModeMixin`
  (in `controller/basecontrollers.py`) and the UI in
  `controller/controllers/SetupModesController.py` /
  `view/widgets/SetupModesWidget.py`
- Legacy per-widget presets in Laser, SLM, and Scan controllers

---

## 1. Audit findings

ImSwitch2 currently has **three overlapping mechanisms** for saving and
restoring widget/hardware state. They were introduced independently and now
duplicate serialization logic, storage, and (worse) disagree on hardware
safety.

### 1.1 Mechanism A — legacy per-widget presets

Each of these is bespoke, with its own file format, storage location, and UI:

| Widget | Save/load API | Storage |
| --- | --- | --- |
| Laser | `makePreset` / `applyPreset`, `savePreset(As)`, `deletePreset`, `loadPreset`; signals `sigPresetSelected`, `sigSavePresetClicked`, … | `setupInfo.laserPresets` (persisted into the **setup config JSON** via `setLaserPreset`) |
| SLM (`SLMController`) | `saveParams` / `loadParams` | standalone JSON file |
| SLMs (`SLMsController`) | `save_hdf5_config` / `load_hdf5_config`, aberration params | HDF5 + JSON |
| Scan advanced (`ScanControllerAdvanced`) | `saveScanParamsToFile` / `loadScanParamsFromFile` (+ legacy INI fallback) | user-picked JSON file |
| TriggerScope raster / LSXYR | `saveScan` / `loadScan` → `saveScanParamsToFile` / `loadScanParamsFromFile` | user-picked file |

Payloads differ widely (Laser preset = `{laserName: LaserPresetInfo(value)}`;
scan preset = `{analogParameterDict, digitalParameterDict}`).

### 1.2 Mechanism B — `WidgetStatePersistence` (global state persistence)

- Interface: controllers implement `getWidgetState() -> dict` /
  `setWidgetState(dict)` (+ optional `getStateSchemaVersion`) and call
  `getWidgetStatePersistence().register(name, self)`.
- Storage: `UserFileDirs.Root/imcontrol_widget_states/<controller>/<state>.json`.
- Consumers in `ImConMainController`:
  - `loadAllWidgetStates('default')` on startup,
  - `saveAllWidgetStates('default')` on shutdown,
  - `save_to_file` / `load_from_file` for a single-file bundle export/import.
- **Safety policy: never auto-applies hardware-active state** (e.g. Laser
  `setWidgetState` restores power values and the *selected preset label* but
  explicitly does NOT enable lasers).
- Registered controllers (10): `LaserController`, `SettingsController`,
  `PositionerController`, `RotatorController`, `RecordingController`,
  `ScanControllerAdvanced`, `ScanControllerBase` (as `ScanController`),
  `ScanControllerMoNaLISA`, `ScanControllerPointScan`, `BeadRecController`,
  plus `GuiLayout` via an adapter.

### 1.3 Mechanism C — `SetupModeController` + `SetupModeMixin` (setup modes)

- Interface: controllers implement `getSetupModeState()` /
  `applySetupModeState(state) -> list[warning]` by inheriting `SetupModeMixin`.
  Discovery is by `isinstance(controller, SetupModeMixin)`.
- Storage: `UserFileDirs.Root/imcontrol_setup_modes/<name>.json`, one file per
  named mode, each carrying `includedComponents` (a user-chosen subset) +
  per-component `state`, metadata, and an optional keyboard `shortcut`.
- Apply is **ordered** by component-local `setupModeApplyPriority` bands. The
  default shipped priorities preserve the legacy relative order (`Settings`,
  `Scan`, `SLMs`, `SLM`, microscope stands such as `LeicaStand`, beam-path
  components such as `FlipMirror`, then `Laser`) without a central device-name
  list. Components that make setup-mode apply safety-critical declare
  `setupModeHardwareCritical = True`, which smart-microscopy mode switching
  reads via the setup-mode backend instead of a central allowlist. It
  **intentionally drives hardware**, gated in the UI by a laser-power
  safety dialog + suppressible warnings
  (`SetupModesController`, `imcontrol_setup_mode_settings.json`).
- **Mode-aware controllers actually wired today: only `SuperScanController`
  (all scan variants, via inheritance) and `FlipMirrorController`.**

### 1.4 The core problems

1. **Two near-identical serialization interfaces per controller.**
   `getWidgetState`/`setWidgetState` (B) and `getSetupModeState`/
   `applySetupModeState` (C) both snapshot/restore controller state to a JSON
   dict. Scan implements **both, with different payload shapes**; Laser
   implements only B; FlipMirror only C.

2. **The headline setup-mode use case is not deliverable.**
   `SetupModesController` contains rich summarizers for `Laser`, `Settings`
   (detector), and `SLMs` state (e.g. `currentPreset`, `scanDefaultPreset`,
   per-laser enabled/value/units; detector ROI/binning/trigger; SLM config
   names). But **Laser, Settings, and SLM do not implement `SetupModeMixin`**,
   so a saved mode can only ever contain Scan + FlipMirror. All the
   Laser/Detector/SLM summarizer/diff code is currently dead.

3. **Opposed and underspecified safety semantics.** B is "never apply
   hardware-active state"; C is "apply hardware, with a power-threshold gate."
   The useful distinction is not simply hardware vs no hardware: startup
   restore may still set hardware parameters such as laser values, detector
   ROI/binning, and scan parameters. The forbidden actions are activation,
   motion, emission, acquisition, and scan start, but that policy is currently
   implicit in each controller.

4. **Three storage roots + a settings file + setup-config entries**
   (`imcontrol_widget_states/`, `imcontrol_setup_modes/`,
   `imcontrol_setup_mode_settings.json`, `setupInfo.laserPresets`), with no
   shared schema/versioning story.

5. **Legacy presets are siloed** per widget with inconsistent UX and formats,
   duplicating what a unified component-state snapshot already captures.

6. **Component names are inconsistent across consumers.** Setup modes use UI
   component keys such as `Laser`, `Settings`, and `Scan`; widget persistence
   uses registration keys such as `LaserController`, `SettingsController`, and
   `ScanControllerAdvanced`. A unified registry needs canonical names and
   legacy aliases, otherwise existing default-state files and exported bundles
   will silently stop applying.

7. **Setup-mode preview and safety are coupled to raw payloads.**
   `SetupModesController` directly parses saved `Laser`, `Settings`, `SLM`,
   and `Scan` dictionaries for summaries, diffs, and high-power warnings. If
   safety becomes consumer-owned, the consumer still needs a stable way to ask
   components what a saved state means and what hazards it contains.

---

## 2. Target design

One serialization contract, one registry, one on-disk schema, two *consumers*.

### 2.1 Single component-state contract

Introduce one mixin (working name `StatefulComponentMixin`) that supersedes
both `SetupModeMixin` and the ad-hoc `getWidgetState`/`setWidgetState`:

```python
from enum import Enum


class ComponentStateApplyMode(Enum):
    STARTUP_RESTORE = "startup_restore"
    SETUP_MODE_APPLY = "setup_mode_apply"


class StatefulComponentMixin:
    stateSchemaVersion = 1
    componentName = None
    legacyStateNames = ()

    def getComponentState(self) -> dict: ...

    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Return warnings; never raise for recoverable mismatches."""

    def describeComponentState(self, state: dict) -> list[str]: ...

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]: ...
```

- `STARTUP_RESTORE` → restore passive UI/configuration state only. It may set
  saved parameters, but it must not enable lasers, start acquisition, move
  hardware, start scans, or otherwise activate output.
- `SETUP_MODE_APPLY` → enact a saved setup mode after the consumer has previewed
  hazards and received any required user confirmation.
- `describeComponentState` and `getComponentStateHazards` keep setup-mode
  inspectors, update previews, and safety dialogs from depending on raw
  component payload schemas.

A single registry (evolve `WidgetStatePersistence` or a new
`ComponentStateRegistry`) holds canonical `componentName -> controller` and
owns snapshot/restore, JSON (de)serialization, schema-version checks,
JSON-serializability assertions, summary/hazard delegation, and legacy-name
aliasing.

Canonical component names should match setup-mode/UI keys where possible:
`Laser`, `Settings`, `Scan`, `SLM`, `SLMs`, `FlipMirror`, `GuiLayout`, etc.
Legacy aliases such as `LaserController`, `SettingsController`,
`ScanControllerAdvanced`, and `ScanControllerPointScan` remain readable for old
default-state files and exported bundles.

### 2.2 Two consumers on the same primitives

- **Global state persistence** = snapshot of *all* registered components,
  auto-saved as `default`, restored on startup with
  `ComponentStateApplyMode.STARTUP_RESTORE`. Bundle export/import remains
  backward-compatible with old `WidgetStatePersistence` files.
- **Setup modes** = named, *partial* bundles (`includedComponents`) restored
  with `ComponentStateApplyMode.SETUP_MODE_APPLY` behind safety preview,
  confirmation, and shortcuts. The setup-mode file format already nests
  per-component state, so it maps directly onto the unified contract.
- **Preview/safety** = consumers ask the registry/components for summaries,
  diffs, and hazards before applying. The UI may still own policy knobs (for
  example high-power thresholds and suppressed warnings), but it should not
  parse component payload internals directly.

### 2.3 Legacy presets

Re-express each legacy preset as a **component-scoped named state** eventually,
but do not force that storage migration in the first rollout. The low-risk
initial end state is:
- Keep existing preset combo/buttons and external file workflows as thin
  wrappers around `getComponentState`/`applyComponentState` where practical.
- Keep old Laser/SLM/Scan preset formats readable.
- Add one-time import or migration shims only when a specific storage format is
  actually retired.

Full storage consolidation can happen later, after the unified contract and
component migrations are proven against existing user files.

---

## 3. Execution plan

Phases are ordered by dependency. Within a phase, **task units are written to
be independent and self-contained** (separate files, minimal shared edits) so
each can be handed to its own agent session. Note: agent sessions in this
project **share one working checkout and run sequentially** — "independent"
here means low cross-task merge risk and any-order schedulability, *not*
concurrent execution.

### Phase 0 — Contract, naming & safety spec (single owner, blocking)
- Ratify the `StatefulComponentMixin` signature, the
  `ComponentStateApplyMode` enum, and exact forbidden actions for
  `STARTUP_RESTORE`.
- Define canonical component names plus legacy aliases for all existing
  persistence keys (`LaserController`, `SettingsController`,
  `ScanControllerAdvanced`, etc.).
- Define the unified on-disk schema (top-level `schemaVersion`,
  per-component `schema_version`) and the old-file read strategy for
  `imcontrol_widget_states/`, setup-mode files, exported bundles, and
  `GuiLayout`/ImProcess layout consumers.
- Define the preview API: component summaries, diffs, and hazard records,
  including how Laser high-power warnings receive threshold/context from the UI.
- Set the legacy-preset policy to "wrap and keep readable first; migrate
  storage later only with explicit import shims."
- Output: a short spec doc that Phase 1 and Phase 2 tasks consume verbatim.
- **Must precede everything else.** Not parallelizable.

### Phase 1 — Unified core + compatibility shims (single owner, blocking)
- Add `StatefulComponentMixin` and the unified registry. Make the registry
  bridge both legacy interfaces (call `getComponentState` if present, else fall
  back to `getWidgetState`/`getSetupModeState`) so nothing breaks mid-migration.
- Make `SetupModeController` and the global-persistence consumers both go
  through the unified registry.
- Preserve the public `getWidgetStatePersistence()` API for current callers,
  including `ImConMainController` and the ImProcess layout consumer.
- Provide deprecation shims for old controller methods and old public methods:
  `getWidgetState`/`setWidgetState`, `getSetupModeState`/
  `applySetupModeState`, `save_to_file`, `load_from_file`, and per-component
  state files.
- Do **not** move storage roots in this phase. Read/write the existing paths,
  with aliases and schema metadata added where possible.
- Add Phase-1 compatibility tests:
  - legacy `getWidgetState` controllers still register, save, and load;
  - legacy setup-mode controllers still snapshot/apply;
  - old widget-state files and exported bundles load through aliases;
  - old setup-mode files still apply in order;
  - `STARTUP_RESTORE` does not activate lasers/acquisition/scans;
  - `GuiLayout` and ImProcess layout keys still work.
- Output: green test suite with old per-controller interfaces and old files
  still working.
- Depends on Phase 0. Not parallelizable (touches shared core).

### Phase 2 — Per-controller migration (independent task units)
Each controller migrates to `getComponentState`/`applyComponentState` and is
registered once. These are the **prime candidates for separate agent
sessions** — each touches a single controller file (+ its test), no shared
edits beyond the registration line already present:

- **2a. Laser** — migrate to component state; add setup-mode activation path
  (enable/value) and summary/hazard reporting. Preserve current preset UI and
  `setupInfo.laserPresets` readability; make preset actions thin wrappers where
  practical but defer storage retirement.
- **2b. Settings (detector)** — make mode-aware; emit ROI/binning/trigger
  payload plus summaries/diffs/hazards. Confirm startup restore never starts
  acquisition.
- **2c. SLM (`SLMController`)** — migrate standalone JSON parameter state to
  the unified contract while keeping existing `saveParams`/`loadParams`
  workflows readable.
- **2d. SLMs (`SLMsController`)** — migrate multi-SLM state and HDF5 config
  references. Store config name/path/metadata in component state; do not embed
  large HDF5 payloads directly in setup-mode JSON.
- **2e. Scan core** — collapse the *two* existing scan payloads
  (`getWidgetState` vs `getSetupModeState`) for `SuperScanController`,
  Advanced, Base, MoNaLISA, and PointScan. Preserve old scan state reads.
- **2f. TriggerScope / LightSheet scan wrappers** — convert `saveScan` /
  `loadScan` and `saveScanParamsToFile` / `loadScanParamsFromFile` callers to
  the unified payload where possible, while keeping old user-picked files
  readable.
- **2g. FlipMirror** — port from `SetupModeMixin` to the unified mixin (small).
- **2h. Positioner / Rotator / Recording / BeadRec** — already B-only; thin
  rename to the unified contract with `STARTUP_RESTORE` support only unless a
  later setup-mode use case is explicitly defined.

Dependency: all of 2a–2h depend only on Phase 1. They are mostly independent,
except 2f should follow 2e because the file wrappers should reuse the scan-core
payload. Sequence 2a→2b→2c→2d→2e→2f→2g→2h is conservative; 2a, 2d, 2e, and 2f
are the largest.

### Phase 3 — Consumer/UX consolidation (after Phase 2)
- **3a.** Wire the now-real Laser/Settings/SLM components into the setup-mode
  component list and route inspector/update-preview/safety dialogs through the
  registry summary/diff/hazard API. Remove component-specific raw-payload
  parsing only after the replacement API covers the same UX.
- **3b.** Convert legacy preset UIs to thin wrappers where not already done.
  Keep old formats readable. Add one-time import shims only for formats that
  are actually retired.
- **3c.** Document the current mixed storage layout and migration guarantees.
  Defer physical storage-root consolidation until compatibility has shipped and
  existing files have a tested migration path.
- 3a depends on 2a/2b/2c/2d and the preview API from Phase 1; 3b depends on the
  relevant 2x; 3c is cross-cutting (single owner).

### Phase 4 — Cleanup & docs
- Remove `SetupModeMixin` and the old `getWidgetState`/`setWidgetState` shims
  only after all controllers, old files, and downstream consumers have migrated
  or have explicit compatibility readers. Update tests
  (`test_widget_state_persistence`, `test_setup_modes`,
  `test_gui_layout_persistence_contract`, plus component-specific migration
  tests) and user docs.
- Optional late task: consolidate storage roots and safety settings under one
  documented layout, guarded by migration tests and old-file import coverage.

---

## 4. Suggested agent task breakdown

Self-contained units suitable for separate (sequential) agent sessions, in
dependency order:

1. `phase0_contract_naming_safety_spec` — write the contract, apply-mode,
   canonical-name/alias, schema, preview, hazard, and legacy-preset policy spec.
   (blocking)
2. `phase1_unified_core` — registry + mixin + compat shims + reroute consumers.
   (blocking)
3. `phase2a_laser_component_state` — Laser component state, activation apply,
   summaries, hazards, and preset-wrapper compatibility.
4. `phase2b_settings_component_state` — detector mode-awareness and no-startup
   acquisition guarantee.
5. `phase2c_slm_component_state` — single-SLM JSON parameter state.
6. `phase2d_slms_component_state` — multi-SLM HDF5 config-reference state.
7. `phase2e_scan_core_component_state` — collapse dual scan payloads for the
   core scan controllers.
8. `phase2f_scan_file_wrappers` — TriggerScope/LightSheet scan save/load
   wrappers on top of the unified scan payload.
9. `phase2g_flipmirror_port` — small mixin port.
10. `phase2h_misc_controllers` — Positioner/Rotator/Recording/BeadRec rename.
11. `phase3a_mode_ui_preview_safety` — activate Laser/Settings/SLM in mode UI
    and replace raw-payload summary/safety parsing with registry APIs.
12. `phase3b_legacy_preset_wrappers` — finish preset wrappers and only add
    import shims for formats that are retired.
13. `phase3c_storage_docs_migration_guardrails` — document mixed storage and
    migration guarantees; defer physical consolidation.
14. `phase4_cleanup_docs` — remove obsolete shims when safe, expand tests, and
    update user docs.

Tasks 3–10 (Phase 2) are the independent, fan-out-friendly block, with task 8
depending on task 7. Everything before is a hard prerequisite and everything
after depends on Phase 2 results.

# Audit Report 3 — Half-migrated Abstractions

**Repository:** /Users/lenny/PycharmProjects/Imswitch2
**Audit Date:** 2026-06-21
**Scope:** Cross-cutting abstraction migration status (state persistence, smart modes, plugins, shortcuts)

## Summary

- **State Persistence (StatefulComponentMixin):** 13 controllers adopted, 32+ controllers still using legacy or no state persistence. Mixed adoption creates dual maintenance burden.
- **Smart Microscopy Mode Switching:** New service + mixin operational, but legacy signal path (`sigSetConfig`, `sigSetVisibleLayers`) remains **live** in 2 key controllers, creating parallel switching mechanisms.
- **Device Plugins:** Registry-backed for 7 manager categories (detectors, lasers, positioners, rotators, rs232, flipMirrors, slms); stands and pulsegen use hardcoded imports. Clean split, low confusion risk.
- **Shortcuts:** Unified ShortcutManager adoption complete; no hardcoded `QShortcut` bypasses detected. This is the only fully-migrated abstraction.

## Per-system Adoption Maps

### 1. State Persistence — StatefulComponentMixin

**Adoption Status:** MIXED (13 adopted / 32+ legacy or none)

#### Adopted (NEW path)
Controllers implementing `StatefulComponentMixin` interface:
- `BeadRecController` — imswitch/imcontrol/controller/controllers/BeadRecController.py:33
- `SLMController` — imswitch/imcontrol/controller/controllers/SLMController.py:20
- `FlipMirrorController` — imswitch/imcontrol/controller/controllers/FlipMirrorController.py:18
- `LeicaStandController` — imswitch/imcontrol/controller/controllers/LeicaStandController.py:18
- `SLMsController` — imswitch/imcontrol/controller/controllers/SLMsController.py:22
- `RecordingController` — imswitch/imcontrol/controller/controllers/RecordingController.py:21
- `RotatorController` — imswitch/imcontrol/controller/controllers/RotatorController.py:15
- `LaserController` — imswitch/imcontrol/controller/controllers/LaserController.py:26
- `TriggerScopeLSXYRController` — imswitch/imcontrol/controller/controllers/TriggerScopeLSXYRController.py:12
- `TriggerScopeRasterController` — imswitch/imcontrol/controller/controllers/TriggerScopeRasterController.py:12
- `PositionerController` — imswitch/imcontrol/controller/controllers/PositionerController.py:15
- `SettingsController` — imswitch/imcontrol/controller/controllers/SettingsController.py:18
- `SuperScanController` — imswitch/imcontrol/controller/basecontrollers.py (base scan controller)

All adopted controllers register via `getWidgetStatePersistence().register()` for unified state management.

#### Not Adopted (LEGACY or NONE)
Controllers without `StatefulComponentMixin`:
- `AlignAverageController` — imswitch/imcontrol/controller/controllers/AlignAverageController.py:6
- `AlignXYController` — imswitch/imcontrol/controller/controllers/AlignXYController.py:6
- `AlignmentLineController` — imswitch/imcontrol/controller/controllers/AlignmentLineController.py:4
- `AutofocusController` — imswitch/imcontrol/controller/controllers/AutofocusController.py:14
- `BFTimelapseController` — imswitch/imcontrol/controller/controllers/BFTimelapseController.py:8
- `BSC203Controller` — imswitch/imcontrol/controller/controllers/BSC203Controller.py:14
- `ConsoleController` — imswitch/imcontrol/controller/controllers/ConsoleController.py:4
- `EtMonalisaController` — imswitch/imcontrol/controller/controllers/EtMonalisaController.py:36
- `EtSTEDController` — imswitch/imcontrol/controller/controllers/EtSTEDController.py:31
- `EtSnoutyController` — imswitch/imcontrol/controller/controllers/EtSnoutyController.py:56 (uses SmartModeRoleMixin only)
- `EventTriggeredControllerBase` — imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py:101
- `FFTController` — imswitch/imcontrol/controller/controllers/FFTController.py:8
- `FLIMHistController` — imswitch/imcontrol/controller/controllers/FLIMHistController.py:6
- `FocusLockController` — imswitch/imcontrol/controller/controllers/FocusLockController.py:13
- `ImageController` — imswitch/imcontrol/controller/controllers/ImageController.py:8
- `LightSheetMulticolorController` — imswitch/imcontrol/controller/controllers/LightSheetMulticolorController.py:11
- `LineProfileController` — imswitch/imcontrol/controller/controllers/LineProfileController.py:20
- `MotCorrController` — imswitch/imcontrol/controller/controllers/MotCorrController.py:4
- `RotationScanController` — imswitch/imcontrol/controller/controllers/RotationScanController.py:13
- `SetupModesController` — imswitch/imcontrol/controller/controllers/SetupModesController.py:14
- `SetupStatusController` — imswitch/imcontrol/controller/controllers/SetupStatusController.py:11 (LEGACY)
- `TilingController` — imswitch/imcontrol/controller/controllers/TilingController.py:17
- `TriggerScopeGalvoDetectionController` — imswitch/imcontrol/controller/controllers/TriggerScopeGalvoDetectionController.py:10
- `TriggerScopePLSRController` — imswitch/imcontrol/controller/controllers/TriggerScopePLSRController.py:10
- `TriggerScopePLSRMulticolorController` — imswitch/imcontrol/controller/controllers/TriggerScopePLSRMulticolorController.py:10
- `TriggerScopeScanController` — imswitch/imcontrol/controller/controllers/TriggerScopeScanController.py:320
- `ULensesController` — imswitch/imcontrol/controller/controllers/ULensesController.py:6
- `ViewController` — imswitch/imcontrol/controller/controllers/ViewController.py:5
- `ViewerToolsController` — imswitch/imcontrol/controller/controllers/ViewerToolsController.py:22
- `WatcherController` — imswitch/imcontrol/controller/controllers/WatcherController.py:7
- `WellPlateController` — imswitch/imcontrol/controller/controllers/WellPlateController.py:7
- `WorkflowFacadeController` — imswitch/imcontrol/controller/controllers/WorkflowFacadeController.py:14

### 2. Smart Microscopy Mode Switching

**Adoption Status:** PARTIAL NEW + LIVE LEGACY (parallel paths coexist)

#### New System (operational)
- `SmartMicroscopyModeService` — imswitch/imcontrol/controller/SmartMicroscopyModeService.py:71
  - Maps runtime roles (scouting, event, resume, idle, validation) to setup modes
  - Applies modes via `SetupModeController.applySetupMode`
  - Returns structured `ApplyResult` with hardware component success/failure tracking

- `SmartModeRoleMixin` — imswitch/imcontrol/controller/controllers/SmartModeRoleMixin.py:28
  - Workflow-agnostic glue for role-based mode switching
  - Preflight hazard checking before arming
  - Role application with pass/fail decision logic

- `SetupModeApplyPriority` bands — imswitch/imcontrol/controller/basecontrollers.py:47-63
  - Declarative ordering for setup mode application
  - Priority bands: DETECTOR_SETTINGS=100, SCAN=200, MULTI_SPATIAL_LIGHT_MODULATOR=300, etc.

#### Adopted Controllers (NEW path)
- `EtSnoutyController` — imswitch/imcontrol/controller/controllers/EtSnoutyController.py:56
  - **DIVERGENCE:** Inherits `SmartModeRoleMixin` + `ImConWidgetController` (NOT `EventTriggeredControllerBase`)
  - Sets `SMART_MODE_WORKFLOW` and `SMART_MODE_REQUIRED_ROLES` class attributes
  - Uses `_applySmartModeRole()`, `_preflightSmartModeRoles()`

- `EventTriggeredControllerBase` — imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py:101
  - Inherits `SmartModeRoleMixin` + `ImConWidgetController`
  - Base class for EtMonalisaController, EtSTEDController
  - Provides smart mode integration for all subclasses

#### Legacy Path (LIVE — still emitted/consumed)

**Signal Definitions** (imswitch/imcontrol/controller/CommunicationChannel.py):
- `sigSetVisibleLayers = Signal(object)` — line 61 (detector name tuple for Snouty setup switching)
- `sigSetConfig = Signal(str)` — line 62 (config name for Snouty setup switching)

**LIVE EMITTERS:**
- `EtSnoutyController` — imswitch/imcontrol/controller/controllers/EtSnoutyController.py
  - Line 273: `self._commChannel.sigSetConfig.emit('Widefield imaging')` — **LIVE** when smart modes disabled
  - Line 274: `self._commChannel.sigSetVisibleLayers.emit((self.detectorFast,))` — **LIVE** always
  - Line 290: `self._commChannel.sigSetConfig.emit(legacyConfigName)` — **LIVE** fallback path

**LIVE CONSUMERS:**
- `SetupStatusController` — imswitch/imcontrol/controller/controllers/SetupStatusController.py
  - Line 91: `self._commChannel.sigSetConfig.connect(...)` — **LIVE** listener
  - Line 195: `self._commChannel.sigSetVisibleLayers.emit((self.tiltedCamName,))` — **LIVE** emitter
  - Line 197: `self._commChannel.sigSetVisibleLayers.emit((self.straightCamName,))` — **LIVE** emitter
  - **Purpose:** Controls flip mirrors, rotation stage, Elliptec slider via hardcoded setups

**Widget:**
- `SetupStatusWidget` — imswitch/imcontrol/view/widgets/SetupStatusWidget.py — **LIVE** (UI for legacy system)

#### Seam: Dual Mode Switching Paths
EtSnoutyController exhibits **dual-path behavior**:
1. **NEW:** Smart mode role switching when `smartMicroscopyModeSwitchingEnabled[workflow]` is true
2. **LEGACY:** Signal emissions (`sigSetConfig`, `sigSetVisibleLayers`) when disabled OR as fallback

This creates a **live legacy escape hatch** that prevents full migration.

### 3. Device Plugins

**Adoption Status:** SPLIT BY CATEGORY (clean boundary)

#### Registry-Backed (NEW plugin system)
Manager categories loaded through `DevicePluginRegistry` (imswitch/imcontrol/model/plugins/registry.py:21):
- `detectors` → plugin kind `detector`
- `lasers` → plugin kind `laser`
- `positioners` → plugin kind `positioner`
- `rotators` → plugin kind `rotator`
- `rs232` → plugin kind `rs232`
- `flipMirrors` → plugin kind `flip_mirror`
- `slms` → plugin kind `slm`

Defined in `SUBMANAGERS_PACKAGE_TO_KIND` mapping (imswitch/imcontrol/model/managers/MultiManager.py:15-23).

**Resolution path** (MultiManager._resolveManagerClass:47-74):
1. Try plugin registry first (`get_default_registry().load_manager_class()`)
2. Fallback to legacy internal import (`imswitch.imcontrol.model.managers.<pkg>.<managerName>`)
3. Raise `UnknownDeviceManagerError` if both fail

#### Hardcoded Imports (LEGACY bespoke loaders)
Manager categories **NOT** using plugin registry:

- **stands** — imswitch/imcontrol/model/managers/StandManager.py:6-27
  - Line 16-19: `importlib.import_module(pythontools.joinModulePath(...))` — **HARDCODED**
  - Loads from `imswitch.imcontrol.model.managers.stands.<managerName>`
  - Has mock fallback for NDA-restricted LeicaDMIManager (lines 21-26)

- **pulsegen** — imswitch/imcontrol/model/managers/pulsegen/
  - No MultiManager, no registry
  - Direct imports in setup code
  - Examples: PulseStreamerManager, TeensyPulseManager

**Rationale:** Documented in docs/design/DEVICE_PLUGINS.md as "MultiManager-backed vs bespoke kinds" (cited in MultiManager.py:14).

#### Seam: Plugin Coverage Gap
Stands and pulsegen cannot use external plugins; all implementations must live in-tree. This is **intentional architectural split**, not accidental half-migration.

### 4. Shortcuts

**Adoption Status:** FULLY MIGRATED (unified system)

#### Unified System
- `ShortcutManager` — imswitch/imcontrol/controller/ShortcutManager.py:69
  - Owns complete shortcut lifecycle: collection, config merge, conflict detection, Qt binding
  - Methods: `collect()`, `registerAction()`, `configure()`, `bind()`, `unbind()`
  - Positioner jog alias expansion via `computePositionerJogDefaults()` (lines 15-66)

#### No Legacy Bypasses Detected
- Searched for `QShortcut` and `QKeySequence` usage outside ShortcutManager
- Found only **UI editor usage** (not binding):
  - SetupModesController.py:708, 788, 799, 801, 809 — QKeySequence for shortcut text parsing
  - SetupModesWidget.py:804-823 — QKeySequenceEdit UI component
  - ShortcutEditorDialog.py:144-218 — QKeySequenceEdit UI component
- No controllers bypass ShortcutManager with direct `QShortcut` instantiation

#### Status: CLEAN
Shortcuts are the **only fully-migrated abstraction** with no legacy seams.

## Findings

### [HIGH] State Persistence Dual Maintenance Burden

**New vs Legacy:** 13 controllers use `StatefulComponentMixin` (unified state snapshots), 32+ controllers lack it entirely or use implicit state (no `getWidgetState`/`setWidgetState` fallback found in code).

**Dead or Live:** The *interface* `getWidgetState`/`setWidgetState` is **dead** (no code emits or consumes it). The **absence** of StatefulComponentMixin means those 32+ controllers have no startup restore or setup mode participation.

**Sites:**
- Interface definition: imswitch/imcontrol/controller/basecontrollers.py:66-177 (StatefulComponentMixin)
- Adoption seam: 13 controllers implement all 4 methods (`getComponentState`, `applyComponentState`, `describeComponentState`, `getComponentStateHazards`)
- Non-adoption: 32+ controllers never implement the mixin (see §1 adoption map)

**Recommendation:**
1. **Mandate StatefulComponentMixin** for all controllers managing persistent hardware state (lasers, detectors, positioners, SLMs, scans, etc.).
2. Provide migration guide: stub implementation for controllers with no persistent state (return empty dict, no-op apply).
3. Mark non-stateful controllers (AlignmentLine, Console, Watcher, etc.) with explicit `NoStateController` marker to distinguish "no state" from "forgot to migrate".
4. Add unit test in CI requiring all hardware-facing controllers inherit StatefulComponentMixin or NoStateController.

**Impact:** Without migration, new setup mode features (hazard checking, apply priority, smart microscopy integration) cannot reach 70% of controllers.

---

### [HIGH] Smart Mode Dual Switching Paths (Live Legacy Escape)

**New vs Legacy:** SmartMicroscopyModeService + SmartModeRoleMixin provide structured role→mode mapping with hazard preflight. Legacy `sigSetConfig`/`sigSetVisibleLayers` signals provide string-based config switching with no preflight or structured results.

**Dead or Live:** **LIVE**. Both paths execute in production:
- NEW path: EtSnoutyController when `smartMicroscopyModeSwitchingEnabled[workflow]=true`
- LEGACY path: EtSnoutyController when disabled (lines 273-274, 290) + SetupStatusController always (lines 91, 195, 197)

**Sites:**
- Legacy signal definitions: imswitch/imcontrol/controller/CommunicationChannel.py:61-62
- Legacy emitters (LIVE):
  - EtSnoutyController.py:273 (`sigSetConfig`)
  - EtSnoutyController.py:274 (`sigSetVisibleLayers`)
  - EtSnoutyController.py:290 (`sigSetConfig` fallback)
  - SetupStatusController.py:195 (`sigSetVisibleLayers`)
  - SetupStatusController.py:197 (`sigSetVisibleLayers`)
- Legacy consumer (LIVE):
  - SetupStatusController.py:91 (`sigSetConfig.connect()`)
- New system:
  - SmartMicroscopyModeService.py:71-358
  - SmartModeRoleMixin.py:28-218
  - EtSnoutyController.py:56 (adopts SmartModeRoleMixin)
  - EventTriggeredControllerBase.py:101 (adopts SmartModeRoleMixin)

**Recommendation:**
1. **Deprecate sigSetConfig/sigSetVisibleLayers** in favor of SmartMicroscopyModeService for ALL workflows.
2. Migrate SetupStatusController's flip mirror / rotation stage logic to a StatefulComponentMixin-based FlipMirrorController setup mode (already exists: FlipMirrorController.py:18).
3. Remove legacy signal emissions from EtSnoutyController (lines 273-274, 290). Gate transition with feature flag if needed for rollback.
4. Add deprecation warning when legacy signals are emitted/connected (log to help identify remaining consumers in external configs).
5. Target removal: 1 release cycle after SetupStatusController migration.

**Impact:** Dual paths complicate debugging ("why didn't my smart mode apply?"), make hazard preflight incomplete (legacy path bypasses it), and prevent unified mode switching audit logs.

---

### [HIGH] EtSnoutyController Architecture Divergence

**New vs Legacy:** EventTriggeredControllerBase provides a unified base for event-triggered workflows (EtMonalisa, EtSTED). EtSnoutyController reimplements the same smart-mode glue (SmartModeRoleMixin) but does NOT inherit EventTriggeredControllerBase.

**Dead or Live:** **LIVE** divergence. Both patterns execute in production:
- EtMonalisaController.py:36 — `class EtMonalisaController(EventTriggeredControllerBase)`
- EtSTEDController.py:31 — `class EtSTEDController(EventTriggeredControllerBase)`
- EtSnoutyController.py:56 — `class EtSnoutyController(SmartModeRoleMixin, ImConWidgetController)` — **NOT** on base

**Sites:**
- Base class: EventTriggeredControllerBase.py:101
- Divergent class: EtSnoutyController.py:56
- Mixin: SmartModeRoleMixin.py:28 (shared by both patterns)

**Recommendation:**
1. **Unify on EventTriggeredControllerBase** for all event-triggered workflows.
2. Refactor EtSnoutyController to inherit EventTriggeredControllerBase (breaking change: may need compatibility shims for Snouty-specific methods).
3. If Snouty has unique lifecycle needs (detector switching, scan source management), hoist those to EventTriggeredControllerBase with opt-in flags OR create SnoutyEventTriggeredBase subclass.
4. Document the design decision: why was Snouty kept separate? If no compelling reason, collapse the divergence.

**Impact:** Divergence creates maintenance burden (bug fixes / features must propagate to 2 patterns), confuses developers ("why doesn't Snouty inherit the base?"), and prevents unified event-triggered controller features (e.g., shared detector log schema, common arming preflight).

---

### [MED] Plugin Coverage Gap: Stands and Pulsegen

**New vs Legacy:** 7 manager categories use DevicePluginRegistry (detectors, lasers, positioners, rotators, rs232, flipMirrors, slms). Stands and pulsegen use hardcoded imports.

**Dead or Live:** **INTENTIONAL SPLIT**. Both paths are live and documented:
- Registry-backed: MultiManager.py:15-23 (`SUBMANAGERS_PACKAGE_TO_KIND`)
- Hardcoded: StandManager.py:16-19 (importlib direct), pulsegen (no MultiManager)
- Documentation: cited in MultiManager.py:14 ("see docs/design/DEVICE_PLUGINS.md")

**Sites:**
- Registry-backed resolution: MultiManager.py:47-74 (`_resolveManagerClass`)
- Hardcoded stand loader: StandManager.py:16-19
- Pulsegen managers: imswitch/imcontrol/model/managers/pulsegen/ (no unified loader)

**Recommendation:**
1. **Accept the split** if documented design decision holds (e.g., stands require NDA-restricted code paths, pulsegen has special timing needs).
2. **Add stands and pulsegen to registry** if no blocking technical reason exists:
   - Define `stand` and `pulse_generator` plugin kinds
   - Migrate StandManager to MultiManager pattern
   - Update DEVICE_PLUGINS.md with migration status
3. If keeping split, add explicit comment in MultiManager.py:15 explaining why stands/pulsegen are excluded (currently only a doc reference, not inline rationale).

**Impact:** Medium severity. External users cannot provide stand/pulsegen plugins, limiting extensibility. But if these categories are rarely extended (e.g., only 1-2 stand types exist), the split may be acceptable.

---

### [MED] StatefulComponentMixin Adoption Seam: Scan Controllers

**New vs Legacy:** SuperScanController (basecontrollers.py) implements StatefulComponentMixin. Several TriggerScope scan controllers (TriggerScopeGalvoDetectionController, TriggerScopePLSRController, TriggerScopePLSRMulticolorController, TriggerScopeScanController) do NOT.

**Dead or Live:** MIXED. Some scan controllers adopted (TriggerScopeLSXYRController, TriggerScopeRasterController), others did not (see above). All are **LIVE** in production.

**Sites:**
- Adopted:
  - TriggerScopeLSXYRController.py:12 — `class TriggerScopeLSXYRController(StatefulComponentMixin, ...)`
  - TriggerScopeRasterController.py:12 — `class TriggerScopeRasterController(StatefulComponentMixin, ...)`
- Not adopted:
  - TriggerScopeGalvoDetectionController.py:10 — NO StatefulComponentMixin
  - TriggerScopePLSRController.py:10 — NO StatefulComponentMixin
  - TriggerScopePLSRMulticolorController.py:10 — NO StatefulComponentMixin
  - TriggerScopeScanController.py:320 — NO StatefulComponentMixin
  - LightSheetMulticolorController.py:11 — NO StatefulComponentMixin

**Recommendation:**
1. **Prioritize scan controller migration** — scan state (analog axes, digital TTL, Nx/Ny, advanced mode flags) is the most complex state in the system.
2. SuperScanController already provides a reference implementation (basecontrollers.py); refactor TriggerScope controllers to inherit or adapt the pattern.
3. Add `ScanStateMixin` helper if TriggerScope controllers share state schema but need custom `applyComponentState` safety logic.

**Impact:** Without unified scan state, setup modes cannot capture/restore full scan configurations for TriggerScope workflows. This limits multi-setup reproducibility (e.g., "save my STED scan params, switch to widefield, then restore STED").

---

### [LOW] SetupStatusController Hardcoded Setup Names

**New vs Legacy:** SetupStatusController hardcodes setup config names (`'Widefield imaging'`, `'Light sheet imaging'`, etc.) at lines 82-88. These names are emitted via `sigSetConfig` and must match string literals in consumer code.

**Dead or Live:** **LIVE** — hardcoded strings drive flip mirror positions and hotkeys (F1-F10).

**Sites:**
- SetupStatusController.py:81-88 (`self.setupConfigs = {...}`)
- EtSnoutyController.py:273 (`emit('Widefield imaging')`)
- EtSnoutyController.py:290 (`emit(legacyConfigName)`)

**Recommendation:**
1. Extract setup config names to a shared constants module or config file.
2. Validate EtSnoutyController emissions against known config names at startup (fail-fast if typo).
3. Long-term: replace string-based config switching with ID-based or enum-based switching (e.g., `SetupConfigID.WIDEFIELD_IMAGING`).

**Impact:** Low severity but brittle. Typo in config name silently fails (no error, just no flip mirror movement). Centralized constants reduce typo risk.

---

### [LOW] No StatefulComponentMixin on View/UI Controllers

**New vs Legacy:** ViewController, ViewerToolsController, AlignmentLineController, ConsoleController, WatcherController are UI-only controllers with no persistent hardware state.

**Dead or Live:** **LIVE** — these controllers are operational but correctly lack state persistence (they manage transient UI, not hardware).

**Sites:**
- ViewController.py:5
- ViewerToolsController.py:22
- AlignmentLineController.py:4
- ConsoleController.py:4
- WatcherController.py:7

**Recommendation:**
1. **Mark as non-stateful** with explicit docstring or base class (`NoStateController` mixin).
2. Add unit test ensuring these controllers do NOT accidentally gain StatefulComponentMixin (they should fail the test if they do).
3. Update developer docs: "UI-only controllers should NOT implement StatefulComponentMixin; hardware controllers MUST."

**Impact:** Low severity. Currently harmless (no state = no StatefulComponentMixin needed), but without explicit marker, future developers may wonder "did we forget to add state persistence?" Explicit marker prevents confusion.

---

## Severity Table

| Finding | Severity | Dead/Live | Controllers Affected | Recommended Action |
|---------|----------|-----------|---------------------|-------------------|
| State Persistence Dual Maintenance | HIGH | Live (32+ controllers lack StatefulComponentMixin) | 32+ controllers (see §1 map) | Mandate StatefulComponentMixin or NoStateController marker; migration guide; CI enforcement |
| Smart Mode Dual Switching Paths | HIGH | Live (both paths execute) | EtSnoutyController, SetupStatusController | Deprecate sigSetConfig/sigSetVisibleLayers; migrate SetupStatusController to FlipMirrorController setup mode; remove legacy emissions |
| EtSnoutyController Architecture Divergence | HIGH | Live (parallel patterns) | EtSnoutyController vs. EtMonalisa/EtSTED | Unify on EventTriggeredControllerBase; document design decision if split is intentional |
| Plugin Coverage Gap (Stands/Pulsegen) | MED | Intentional split (documented) | StandManager, pulsegen managers | Accept split if documented; otherwise add to registry; inline comment explaining exclusion |
| StatefulComponentMixin Scan Controller Seam | MED | Mixed (some adopted, some not) | 4 TriggerScope controllers, LightSheetMulticolorController | Prioritize scan controller migration; add ScanStateMixin if needed |
| SetupStatusController Hardcoded Setup Names | LOW | Live (string-based switching) | SetupStatusController, EtSnoutyController | Extract to shared constants; validate at startup; long-term: ID-based switching |
| No StatefulComponentMixin on View/UI Controllers | LOW | Live (correct absence) | ViewController, ViewerToolsController, etc. | Mark as non-stateful with explicit docstring/marker; unit test to prevent accidental state addition |

---

## Conclusion

ImSwitch2's cross-cutting abstractions show **clear intent** but **incomplete adoption**:

1. **StatefulComponentMixin (state persistence):** 13/45+ controllers adopted. The interface is sound, but 70% of controllers lack it. This is the **highest-priority migration** to unlock setup mode features across the codebase.

2. **SmartMicroscopyModeService (smart modes):** Operational and well-designed, but **live legacy escape hatch** (`sigSetConfig`/`sigSetVisibleLayers`) prevents full migration. Dual paths create confusion and bypass preflight safety checks. **Second-highest priority.**

3. **DevicePluginRegistry (plugins):** Clean split between registry-backed (7 categories) and hardcoded (stands, pulsegen). **Intentional design decision**; low urgency unless extensibility is needed.

4. **ShortcutManager (shortcuts):** **Fully migrated, no legacy seams.** This is the **success story** demonstrating complete abstraction adoption.

**Key Insight:** The codebase exhibits *transition architecture* — new systems are built and proven (SmartModeRoleMixin, StatefulComponentMixin, ShortcutManager) but legacy paths remain **live** (sigSetConfig, 32+ controllers without state). The next phase should **enforce adoption** (CI tests, migration mandates) and **remove escape hatches** (deprecate legacy signals) to complete the migration.

**No modifications made** per audit constraints. Report complete.

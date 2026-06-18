# State Persistence Contract Specification

**Status:** Phase 0 deliverable  
**Reference:** [state_persistence_unification_plan.md](state_persistence_unification_plan.md)  
**Purpose:** Normative contract for Phase 1 implementation and Phase 2 component migrations

---

## 0. Design decisions (2026-06-18 revision — OVERRIDES conflicting statements below)

Two maintainer decisions supersede earlier text wherever they conflict. Phase 2
tasks MUST follow this section over §2–§7 where they differ.

### D1 — No setup-mode membership flag; every component is mode-eligible, each mode selects a subset

There is NO `setupModeParticipant`/opt-in attribute. Every controller that
implements `StatefulComponentMixin` and is registered in the unified registry is
offered as a candidate component when saving a setup mode. The existing per-mode
`includedComponents` list (already in the setup-mode data model and the
Save-mode dialog's "Include in snapshot" checkboxes) determines which components
a given mode snapshots and applies; all components NOT in that list are left
untouched when the mode is applied.

Consequences:
- `SetupModeController` discovery changes from `isinstance(controller,
  SetupModeMixin)` to "all components registered in the unified registry that
  implement `StatefulComponentMixin`" (queried via the registry).
- **Exception:** `GuiLayout` stays STARTUP_RESTORE-only and is NOT offered in
  setup modes (it is a window-dock-layout adapter with no hardware semantics).
- This removes the membership mechanism; any earlier "mode-eligible by
  interface/marker" wording is superseded.

### D2 — No backward compatibility with existing on-disk state/preset files

The feature is new; existing files will be regenerated, not migrated. Therefore:
- Migrated controllers REPLACE their legacy methods outright: delete
  `getWidgetState`/`setWidgetState` (and `getSetupModeState`/
  `applySetupModeState` for Scan/FlipMirror) and implement ONLY the four
  `StatefulComponentMixin` methods.
- Migrated controllers register under their CANONICAL name (e.g.
  `register('Laser', self)`); legacy registration keys are dropped.
- `applyComponentState` does NOT parse legacy payload shapes, and the registry
  does NOT need legacy-alias file-path search. The alias table /
  `legacyStateNames` may remain as harmless no-ops during the transition and be
  removed in Phase 4.
- The Phase 1 consumer-aware routing (WIDGET vs SETUP_MODE) is still required
  DURING the transition window (some controllers migrated, others still exposing
  dual legacy interfaces) and collapses once all controllers are migrated.
- This supersedes the backward-compat requirements in §4.3, §6, and §7.1/§7.3
  (those now apply only to the in-session transition, not to old files on disk).

---

## 1. Component State Contract

### 1.1 ComponentStateApplyMode Enum

```python
from enum import Enum

class ComponentStateApplyMode(Enum):
    """Distinguishes passive UI restore from active hardware application."""
    STARTUP_RESTORE = "startup_restore"
    SETUP_MODE_APPLY = "setup_mode_apply"
```

**String values:** The enum members MUST serialize to the exact strings `"startup_restore"` and `"setup_mode_apply"` for JSON compatibility.

### 1.2 StatefulComponentMixin

```python
class StatefulComponentMixin:
    """Mixin for controllers that provide unified component state snapshots.
    
    Supersedes both the legacy getWidgetState/setWidgetState interface and
    the SetupModeMixin interface. A single component state payload serves
    both startup persistence and named setup modes, with applyMode controlling
    which activations are allowed.
    """
    
    # Class attributes
    stateSchemaVersion: int = 1
    componentName: str | None = None
    legacyStateNames: tuple = ()
    
    def getComponentState(self) -> dict:
        """Snapshot the current component state.
        
        Returns:
            A JSON-serializable dict. The schema is component-specific but
            must remain compatible within a stateSchemaVersion.
        
        Raises:
            May raise for unrecoverable failures (e.g., required hardware
            unavailable). The registry logs and skips the component.
        """
        raise NotImplementedError
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Restore component state from a snapshot.
        
        Args:
            state: A dict previously returned by getComponentState().
            applyMode: Controls which actions are permitted (see Section 2).
        
        Returns:
            A list of warning strings for recoverable issues (e.g., missing
            devices, value clamps, skipped unavailable presets). An empty list
            indicates full success.
        
        Raises:
            MUST NOT raise for recoverable schema mismatches, missing optional
            keys, or unavailable devices. Return a warning instead.
            MAY raise for catastrophic errors (e.g., corrupted state that
            cannot be parsed at all), but this should be rare.
        
        Safety:
            The component MUST enforce the apply-mode safety policy (Section 2).
            The consumer provides the mode; the component owns enforcement.
        """
        raise NotImplementedError
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable summary of a saved state.
        
        Args:
            state: A dict previously returned by getComponentState().
        
        Returns:
            A list of formatted strings suitable for display in a setup-mode
            inspector or update-preview dialog. May be multi-line; each string
            is one logical block.
        
        Examples:
            ["Laser 488nm: ON, 50.0 mW", "Laser 561nm: OFF"]
            ["Detector Camera1: ROI=(0,0,512,512), binning=2x2"]
        
        Notes:
            This replaces the component-specific raw-payload parsing currently
            in SetupModesController._summarizeSaved* methods. The consumer
            calls this instead of parsing the state dict directly.
        """
        raise NotImplementedError
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        """Identify potential hazards in a saved state before applying it.
        
        Args:
            state: A dict previously returned by getComponentState().
            applyMode: The mode in which the state would be applied.
            context: Optional consumer-provided context (e.g., UI thresholds,
                suppressed-warning lists). See Section 5.2 for schema.
        
        Returns:
            A list of hazard records (see Section 5.3 for schema). An empty
            list means no hazards detected.
        
        Examples:
            High-power laser: severity="warning", kind="high_laser_power"
            Missing device: severity="info", kind="device_unavailable"
        
        Notes:
            The component identifies hazards; the consumer owns the policy
            (thresholds, confirmation dialogs, suppression). This replaces
            the current SetupModesController._getHighPowerLaserEntries logic.
        """
        raise NotImplementedError
```

### 1.3 Method Signatures Summary

| Method | Returns | Raises on recoverable mismatch? |
|--------|---------|--------------------------------|
| `getComponentState()` | `dict` | No (may raise for catastrophic hardware failure) |
| `applyComponentState(state, *, applyMode)` | `list[str]` (warnings) | No |
| `describeComponentState(state)` | `list[str]` (summary lines) | No |
| `getComponentStateHazards(state, *, applyMode, context)` | `list[dict]` (hazard records) | No |

---

## 2. Apply-Mode Safety Policy

### 2.1 STARTUP_RESTORE Mode

**Purpose:** Restore passive UI and configuration state on application launch or file import.

**Permitted actions:**
- Set laser power values (the numeric parameter)
- Set laser modulation frequency and duty cycle parameters
- Set detector ROI, binning, and trigger mode
- Restore scan parameter dictionaries (analogParameterDict, digitalParameterDict)
- Restore SLM configuration paths and parameter references
- Restore positioner/rotator position values (if the controller chooses)
- Restore UI selection state (e.g., selected laser preset label, scan mode checkboxes)
- Restore widget layout state (dock positions, splitter sizes)

**Forbidden actions:**
- Enable lasers or emit light
- Start detector acquisition or live preview
- Move positioners, rotators, or other motorized stages
- Start a scan
- Flip mirrors or change active beam paths
- Any other action that activates hardware output or initiates motion

**Rationale:** Startup restore is automatic and unattended. The user must explicitly enable hardware after launch. The distinction is not "hardware vs no hardware" but "passive parameters vs active commands."

**Enforcement:** The component controller MUST check `applyMode == ComponentStateApplyMode.STARTUP_RESTORE` and skip forbidden actions. The consumer (WidgetStatePersistence / unified registry) provides the mode; the component enforces it.

**Example (Laser):**
```python
def applyComponentState(self, state, *, applyMode):
    warnings = []
    # Always restore power values
    for laserName, value in state.get('laser_values', {}).items():
        self.setLaserValue(laserName, value)
    
    # Only enable lasers in SETUP_MODE_APPLY
    if applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
        for laserName, enabled in state.get('laser_enabled', {}).items():
            self.setLaserEnabled(laserName, enabled)
    elif state.get('laser_enabled'):
        warnings.append("Laser enable states not restored in startup mode.")
    
    return warnings
```

### 2.2 SETUP_MODE_APPLY Mode

**Purpose:** Enact a complete saved setup mode, including hardware activation.

**Permitted actions:**
- All actions from STARTUP_RESTORE
- Enable/disable lasers and emit light at saved power levels
- Start or configure detector acquisition if the saved state included it
- Move positioners/rotators to saved positions if the saved state included them
- Flip mirrors to saved positions
- Any other activation or motion that was explicitly saved in the setup mode

**Safety gates (consumer-side):**
- The consumer (SetupModesController) MUST call `getComponentStateHazards()` before applying.
- If hazards with `severity="warning"` or `severity="critical"` are returned, the consumer SHOULD present a confirmation dialog.
- High-power laser warnings: threshold and suppression list come from `imcontrol_setup_mode_settings.json` (see Section 5.2).
- Shortcut-triggered apply: if `confirmHighPowerShortcutApply=true`, require confirmation even for suppressed warnings.

**Enforcement:** The component MAY assume that hazards have been previewed if `applyMode == SETUP_MODE_APPLY`. The component SHOULD still apply state safely (e.g., ramp laser power, check interlock status) but MAY perform activations.

**Forbidden actions (even in SETUP_MODE_APPLY):**
- None, beyond what the component itself deems unsafe (e.g., applying a scan to a missing positioner, which returns a warning instead of activating).

### 2.3 Safety Policy Table

| Action | STARTUP_RESTORE | SETUP_MODE_APPLY |
|--------|----------------|------------------|
| Set laser power value | ✅ Allowed | ✅ Allowed |
| Enable laser | ❌ Forbidden | ✅ Allowed (after hazard preview) |
| Set detector ROI/binning | ✅ Allowed | ✅ Allowed |
| Start acquisition | ❌ Forbidden | ✅ Allowed (if saved state included it) |
| Restore scan parameters | ✅ Allowed | ✅ Allowed |
| Start scan | ❌ Forbidden | ✅ Allowed (if saved state included it) |
| Set positioner position value | ✅ Allowed | ✅ Allowed |
| Move positioner | ❌ Forbidden | ✅ Allowed (if saved state included it) |
| Set SLM config path | ✅ Allowed | ✅ Allowed |
| Flip mirror position | ❌ Forbidden | ✅ Allowed |

---

## 3. Canonical Component Names and Legacy Aliases

### 3.1 Naming Principle

Each component has:
- **Canonical name:** The single authoritative `componentName` class attribute.
- **Legacy WidgetStatePersistence key:** The string passed to `register(key, self)` in the old system.
- **Legacy SetupMode key:** The component key used in `SetupModeController.applyOrder` and setup-mode JSON files.

The unified registry MUST resolve legacy aliases to canonical names so that existing `imcontrol_widget_states/` files, setup-mode JSON files, and exported bundles continue to load.

### 3.2 Canonical Name Table

| Canonical Name | Legacy WidgetStatePersistence Key | Legacy SetupMode Key | Current Implementation | Source File(s) |
|----------------|-----------------------------------|----------------------|------------------------|----------------|
| **Laser** | `LaserController` | `Laser` | `getWidgetState`/`setWidgetState` only | `LaserController.py:79` (register), `SetupModeController.py:29` (applyOrder) |
| **Settings** | `SettingsController` | `Settings` | `getWidgetState`/`setWidgetState` only | `SettingsController.py:97` (register), `SetupModeController.py:23` (applyOrder) |
| **Scan** | `ScanController` (from `ScanControllerBase`), `ScanControllerAdvanced`, `ScanControllerMoNaLISA`, `ScanControllerPointScan` | `Scan` | Both: `getWidgetState`/`setWidgetState` AND `getSetupModeState`/`applySetupModeState` (via `SuperScanController`) | `ScanControllerBase.py:30` (register as `ScanController`), `ScanControllerAdvanced.py:83`, `ScanControllerMoNaLISA.py:55`, `ScanControllerPointScan.py:24`, `SetupModeController.py:24` (applyOrder), `basecontrollers.py:105,340,366` (SuperScanController) |
| **SLM** | *(not currently registered)* | `SLM` | *(not currently implemented)* | `SetupModeController.py:26` (applyOrder), `SetupModesController.py:459` (summarizer exists) |
| **SLMs** | *(not currently registered)* | `SLMs` | *(not currently implemented)* | `SetupModeController.py:25` (applyOrder), `SetupModesController.py:459` (summarizer exists) |
| **FlipMirror** | *(not currently registered for WidgetStatePersistence)* | `FlipMirror` | `getSetupModeState`/`applySetupModeState` only | `FlipMirrorController.py:6` (SetupModeMixin), `SetupModeController.py:28` (applyOrder) |
| **LeicaStand** | *(not currently registered)* | `LeicaStand` | *(not currently implemented)* | `SetupModeController.py:27` (applyOrder), `LeicaStandController.py` (exists) |
| **Positioner** | `PositionerController` | *(not in setup modes)* | `getWidgetState`/`setWidgetState` only | `PositionerController.py:81` (register) |
| **Rotator** | `RotatorController` | *(not in setup modes)* | `getWidgetState`/`setWidgetState` only | `RotatorController.py:36` (register) |
| **Recording** | `RecordingController` | *(not in setup modes)* | `getWidgetState`/`setWidgetState` only | `RecordingController.py:81` (register) |
| **BeadRec** | `BeadRecController` | *(not in setup modes)* | `getWidgetState`/`setWidgetState` only | `BeadRecController.py:122` (register) |
| **GuiLayout** | `GuiLayout` | *(not in setup modes; STARTUP_RESTORE-only)* | `getWidgetState`/`setWidgetState` via `_GuiLayoutStateAdapter` | `ImConMainController.py:106` (adapter registration) |

**Notes:**
- **Scan variants:** All scan controllers (Base, Advanced, MoNaLISA, PointScan) inherit from `SuperScanController`, which implements both interfaces. `ScanControllerBase` registers as `ScanController` (no suffix); the others register with their full class names. The setup-mode component key is always `Scan`. The unified registry MUST map all four legacy keys to the canonical name `Scan` and dispatch to the appropriate controller instance.
- **GuiLayout:** This is an adapter, not a SetupModeMixin controller. It MUST remain STARTUP_RESTORE-only and MUST NOT be offered in the setup-mode component list. ImProcess also consumes widget state persistence for its layout; the registry MUST preserve compatibility.
- **SLM vs SLMs:** Two separate components. `SLM` (singular) is `SLMController` for a single SLM. `SLMs` (plural) is `SLMsController` for multi-SLM setups. Both appear in the setup-mode summarizers (`SetupModesController.py:459,560`) but neither currently implements the mixin.

### 3.3 Alias Resolution Rules

1. **Registry lookup:** When loading a state file or bundle, the registry MUST first try the canonical name, then fall back to each legacy alias in the `legacyStateNames` tuple.
2. **Multiple legacy keys for Scan:** The registry MUST accept `ScanController`, `ScanControllerAdvanced`, `ScanControllerMoNaLISA`, and `ScanControllerPointScan` as aliases for the canonical `Scan` component.
3. **Case sensitivity:** All lookups are case-sensitive. The legacy keys use the exact spelling from the register calls and setup-mode files.
4. **Deprecated key warning:** When a legacy alias is resolved, the registry SHOULD log a debug message noting the deprecated key but MUST NOT fail.

---

## 4. On-Disk Schema and Read Strategy

### 4.1 Unified Schema (Target)

The unified registry uses this schema for new saves, but MUST read legacy formats during the transition.

**Top-level schemaVersion:**
```json
{
  "schemaVersion": 1,
  "components": {
    "Laser": {
      "schema_version": 1,
      "state": { ... }
    },
    "Settings": {
      "schema_version": 1,
      "state": { ... }
    }
  }
}
```

**Per-component schema_version:** Each component dict includes its own `schema_version` (from `stateSchemaVersion` class attribute). This allows individual components to evolve their payload schemas independently.

**Metadata keys:**
- Top-level `schemaVersion`: integer, currently 1. Increment only if the top-level structure changes (e.g., the `components` dict moves or the per-component wrapper changes).
- Per-component `schema_version`: integer, defaults to 1. The component class defines `stateSchemaVersion` and is responsible for backward-compatible reads if it increments.

### 4.2 Legacy Storage Roots (Preserved in Phase 1)

**No storage-root consolidation in Phase 0 or Phase 1.** All existing paths remain readable and writable:

| Storage Artifact | Path | Format | Notes |
|------------------|------|--------|-------|
| Per-controller widget states | `UserFileDirs.Root/imcontrol_widget_states/<controller>/<state>.json` | `{"_metadata": {"controller_name": str, "schema_version": int}, "state": dict}` | Written by `WidgetStatePersistence.saveWidgetState()`. One file per controller per state name. |
| Setup-mode files | `UserFileDirs.Root/imcontrol_setup_modes/<name>.json` | `{"schemaVersion": 1, "name": str, "includedComponents": list, "state": {componentKey: dict}, ...}` | Written by `SetupModeController.saveSetupMode()`. One file per mode. Component keys are legacy setup-mode keys (e.g., `Laser`, `Scan`). |
| Setup-mode settings | `UserFileDirs.Root/imcontrol_setup_mode_settings.json` | `{"warnAboveLaserPowerThreshold": bool, "laserPowerThresholdMw": float, "confirmHighPowerShortcutApply": bool, "suppressedWarnings": list}` | Written by `SetupModesController._saveSafetySettings()`. Single file, not versioned. |
| Single-file bundle (export/import) | User-specified path | `{<controller_name>: {"_metadata": {...}, "state": dict}, ...}` | Written by `WidgetStatePersistence.save_to_file()`. One file, all controllers. Controller keys are legacy WidgetStatePersistence keys. |

**Phase 1 read strategy:** The unified registry MUST read from the existing paths and use the alias table (Section 3.2) to resolve component names. No files move in Phase 1.

**Phase 3c consolidation (deferred):** Storage-root consolidation is deferred to Phase 3c. When consolidation occurs, the old paths MUST remain readable via explicit migration shims, and the migration MUST be tested.

### 4.3 Legacy Format Compatibility

The registry MUST read these legacy artifacts without modification:

1. **Per-controller widget state files:** `imcontrol_widget_states/LaserController/default.json`
   - Schema: `{"_metadata": {"controller_name": "LaserController", "schema_version": 1}, "state": {laser_values: {...}}}`
   - Alias resolution: `LaserController` → canonical `Laser`.

2. **Setup-mode files:** `imcontrol_setup_modes/MyMode.json`
   - Schema: `{"schemaVersion": 1, "name": "MyMode", "state": {"Laser": {...}, "Scan": {...}}}`
   - Alias resolution: `Laser` → canonical `Laser`, `Scan` → canonical `Scan`.
   - Apply order: `SetupModeController.applyOrder = ['Settings', 'Scan', 'SLMs', 'SLM', 'LeicaStand', 'FlipMirror', 'Laser']`.

3. **Single-file bundles:** `my_export.json`
   - Schema: `{"LaserController": {"_metadata": {...}, "state": {...}}, "ScanController": {...}}`
   - Alias resolution: `LaserController` → `Laser`, `ScanController` → `Scan`.

4. **GuiLayout and ImProcess layout keys:** The registry MUST preserve the `GuiLayout` key for ImConMainController's `_GuiLayoutStateAdapter` and MUST NOT break ImProcess's layout persistence (which also uses `getWidgetStatePersistence().register(...)`).

### 4.4 Schema Version Handling

**Component schema mismatch:**
- If `saved_version != current_version`, log a warning but attempt to apply.
- The component's `applyComponentState()` MUST handle missing or renamed keys gracefully (return warnings, not exceptions).

**Top-level schema mismatch:**
- If `schemaVersion` in a setup-mode file or bundle is not 1, the registry MUST reject the file and log an error (the current `SetupModeController._readModeFile` already does this).

---

## 5. Preview, Summary, and Hazard API

### 5.1 Motivation

The current `SetupModesController` directly parses saved `Laser`, `Settings`, `SLM`, and `Scan` payloads to generate summaries, diffs, and high-power warnings. This couples the UI to component-specific schemas. The unified contract externalizes this logic via `describeComponentState()` and `getComponentStateHazards()`.

### 5.2 Context Parameter for Hazard Detection

The consumer (SetupModesController or future setup-mode UI) passes a `context` dict to `getComponentStateHazards()` containing UI policy settings:

**Schema:**
```python
context = {
    "laserPowerThresholdMw": 50.0,  # from imcontrol_setup_mode_settings.json
    "suppressedWarnings": ["Laser:488nm:high_power"],  # from imcontrol_setup_mode_settings.json
    "applySource": "shortcut" | "manual",  # how the apply was triggered
    # Future: positioner motion limits, detector framerate warnings, etc.
}
```

**Laser high-power threshold:** The Laser component reads `context.get("laserPowerThresholdMw", 50.0)` and returns a hazard record for any enabled laser with `value > threshold` and units in `{"mW", "milliwatt", "milliwatts", None, ""}` (see `SetupModesController._getHighPowerLaserEntries` logic at line 1200).

**Suppressed warnings:** The consumer filters hazard records by checking if `f"{componentName}:{details['laserName']}:high_power"` is in `suppressedWarnings`. The component itself does NOT suppress; it always returns all hazards.

### 5.3 Hazard Record Schema

Each hazard is a JSON-serializable dict:

```python
{
    "kind": str,          # Machine-readable hazard type (e.g., "high_laser_power", "device_unavailable")
    "severity": str,      # "info" | "warning" | "critical"
    "message": str,       # Human-readable summary (e.g., "Laser 488nm: 150.0 mW exceeds 50.0 mW threshold")
    "details": dict,      # Component-specific details for suppression keys and display
}
```

**Example (high-power laser):**
```python
{
    "kind": "high_laser_power",
    "severity": "warning",
    "message": "Laser 488nm: 150.0 mW exceeds 50.0 mW threshold",
    "details": {
        "laserName": "488nm",
        "value": 150.0,
        "units": "mW",
        "threshold": 50.0,
    }
}
```

**Severity levels:**
- `"info"`: Informational (e.g., "Positioner X will move 10 mm"). No confirmation required.
- `"warning"`: User should be aware; may require confirmation depending on consumer policy.
- `"critical"`: Strongly recommend confirmation or intervention (e.g., "Laser power > 500 mW").

**Suppression key construction (Laser example):** `f"Laser:{details['laserName']}:high_power"` (the consumer builds this from the hazard record and checks it against `suppressedWarnings` in `imcontrol_setup_mode_settings.json`).

### 5.4 Mapping Existing Summarizer Logic to Components

The following methods in `SetupModesController` (lines 441–1249) MUST be LIFTED into the corresponding component's `describeComponentState()` and `getComponentStateHazards()` methods during Phase 2:

| SetupModesController Method(s) | Target Component | Phase 2 Task |
|-------------------------------|------------------|--------------|
| `_summarizeSavedDetectorState` (line 471) | **Settings** | 2b |
| `_summarizeDetectorChange` (line 867) | **Settings** | 2b |
| `_summarizeSavedLaserState` (line 494), `_savedLaserItemsInDisplayOrder` (line 520), `_currentLaserDisplayOrder` (line 539) | **Laser** | 2a |
| `_summarizeLaserChange` (line 905) | **Laser** | 2a |
| `_getHighPowerLaserEntries` (line 1200), `_isMilliwattUnit` (line 1233), `_laserPowerThresholdMw` (line 1240) | **Laser** (`getComponentStateHazards`) | 2a |
| `_summarizeSavedSLMState` (line 560) | **SLM** / **SLMs** | 2c, 2d |
| `_summarizeSLMChange` (line 942) | **SLM** / **SLMs** | 2c, 2d |
| `_summarizeSavedScanState` (line 573), `_summarizeSavedScanMode` (line 642), `_summarizeSavedScanAxes` (line 658), `_summarizeSavedScanDigitalOverview` (line 695), `_summarizeSavedScanTTL` (line 711), `_summarizeSavedScanExtras` (line 784) | **Scan** | 2e |
| `_summarizeParameterChanges` (line 960) | **Scan** (and possibly Settings) | 2e, 2b |

**Implementation notes:**
- **Lift, do not rewrite:** The logic is already correct. Copy the code into the component, adapt parameter names (e.g., `state` instead of `mode.get("state").get("Laser")`), and test.
- **Helper methods:** Methods like `_onOff`, `_fmt`, `_asFloat`, `_countSavedLeaves` can be factored into a shared utility module if desired, or duplicated into each component if they are small.
- **Display order (Laser):** `_currentLaserDisplayOrder` accesses `self._setupModeController._controllers` to get the Laser controller instance. In the component, this becomes simpler: the Laser component already has `self._master.lasersManager` and `self._widget.laserModules`.

### 5.5 Consumer Responsibilities

**SetupModesController (Phase 1):** Continue to use the old summarizers until Phase 2 lifts them. In Phase 1, the registry provides a compatibility bridge: if a component has `describeComponentState`, call it; else fall back to the old SetupModesController logic.

**SetupModesController (Phase 3a):** After Phase 2, replace raw-payload parsing with:
```python
summary = component.describeComponentState(state)
hazards = component.getComponentStateHazards(
    state,
    applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    context={
        "laserPowerThresholdMw": self._safetySettings["laserPowerThresholdMw"],
        "suppressedWarnings": self._safetySettings["suppressedWarnings"],
        "applySource": "manual",
    }
)
```

**Diff helper (registry-level):** A registry-level helper for diffs is recommended but not specified here. It can call `describeComponentState` on both old and new states and compute a line diff. The existing `_summarizeComponentChange` methods (lines 852, 867, 905, 942) can be similarly lifted or made registry utilities.

---

## 6. Legacy Preset Policy

**Policy:** Wrap and keep old formats readable first; migrate storage later only with explicit import shims.

### 6.1 Legacy Preset Formats in Scope

| Component | Legacy Format | Storage | Read/Write API | Phase 2 Task |
|-----------|---------------|---------|----------------|--------------|
| **Laser** | `setupInfo.laserPresets` | Setup config JSON (persisted via `ConfigFileManager.setLaserPreset`) | `LaserController.makePreset`, `applyPreset`, `savePreset`, `loadPreset`, `deletePreset`, signals `sigPresetSelected`, `sigSavePresetClicked`, etc. | 2a: Make preset actions thin wrappers over component state snapshots where practical; keep `setupInfo.laserPresets` readable; defer storage retirement. |
| **SLM** | Standalone JSON parameter files | User-specified path | `SLMController.saveParams`, `loadParams` | 2c: Preserve `saveParams`/`loadParams` workflows; read old files; defer format migration. |
| **SLMs** | HDF5 config files + JSON metadata | User-specified path (HDF5) | `SLMsController.save_hdf5_config`, `load_hdf5_config`, aberration params | 2d: Store config name/path/metadata in component state; do NOT embed large HDF5 payloads in setup-mode JSON; preserve HDF5 readability. |
| **Scan** | JSON parameter files + legacy INI fallback | User-specified path | `ScanControllerAdvanced.saveScanParamsToFile`, `loadScanParamsFromFile` | 2e: Preserve JSON/INI reads; unify with setup-mode scan payload. |
| **TriggerScope / LightSheet scan wrappers** | `saveScan` / `loadScan` files | User-specified path | `saveScan`, `loadScan`, delegates to `saveScanParamsToFile`/`loadScanParamsFromFile` | 2f: Convert to unified scan payload where possible; keep old user-picked files readable. |

### 6.2 Compatibility Guarantees

1. **Old preset files remain readable.** If a user has a `laser_params.json` from `SLMController.saveParams`, it MUST load in Phase 2 and beyond.
2. **Old UI workflows preserved.** The "Save Preset" / "Load Preset" buttons in the Laser widget MUST continue to work. They MAY be thin wrappers over `getComponentState` / `applyComponentState`, but the user experience does not change.
3. **No automatic migration.** Do NOT rewrite old files in place. If a new unified format is introduced, provide an explicit import/export tool or migration command, tested against real user data.
4. **Storage retirement deferred.** Even if Phase 2 implements component state wrappers, the legacy preset storage (e.g., `setupInfo.laserPresets` in the setup config JSON) remains writable until Phase 3b explicitly retires it with a migration shim.

---

## 7. Migration and Compatibility Checklist

**Purpose:** Phase 1 implementation MUST pass tests asserting every item on this checklist. Phase 2 MUST preserve these guarantees as components migrate.

### 7.1 Phase 1 Compatibility Requirements

1. **Legacy `getWidgetState` controllers still register, save, and load.**
   - All 11 controllers currently registered (Laser, Settings, Scan variants, Positioner, Rotator, Recording, BeadRec, GuiLayout) MUST register with the unified registry via their legacy keys.
   - Calling `loadAllWidgetStates('default')` MUST read the existing `imcontrol_widget_states/<controller>/default.json` files via the alias table.
   - Calling `saveAllWidgetStates('default')` MUST write to the same paths (no storage root change).

2. **Legacy setup-mode controllers still snapshot and apply.**
   - `SuperScanController` and `FlipMirrorController` (the only two current `SetupModeMixin` implementers) MUST continue to work via `getSetupModeState` / `applySetupModeState`.
   - Calling `SetupModeController.saveSetupMode('Test', ['Scan', 'FlipMirror'])` MUST snapshot both components.
   - Calling `SetupModeController.loadSetupMode('Test')` MUST apply both components in order (`Settings`, `Scan`, ..., `FlipMirror`, `Laser`).

3. **Old widget-state files and exported bundles load via aliases.**
   - An old `imcontrol_widget_states/LaserController/default.json` file MUST load into the canonical `Laser` component.
   - An old `imcontrol_widget_states/ScanController/default.json` file (from `ScanControllerBase`) MUST load into the canonical `Scan` component.
   - An exported bundle `my_states.json` with keys `{"LaserController": {...}, "ScanControllerAdvanced": {...}}` MUST load via aliases.

4. **Old setup-mode files still apply in order.**
   - An existing `imcontrol_setup_modes/MyMode.json` file with `"state": {"Laser": {...}, "Scan": {...}}` MUST load and apply.
   - Apply order MUST be preserved: `Settings`, `Scan`, `SLMs`, `SLM`, `LeicaStand`, `FlipMirror`, `Laser`.

5. **STARTUP_RESTORE activates nothing.**
   - Test: Load a Laser state with `{"laser_values": {"488nm": 100.0}, "laser_enabled": {"488nm": true}}` in STARTUP_RESTORE mode. Assert that the laser power is set to 100.0 but the laser is NOT enabled.
   - Test: Load a Scan state with a scan-running flag in STARTUP_RESTORE mode. Assert that the scan parameters are restored but the scan does NOT start.
   - Test: Load a Positioner state with a target position in STARTUP_RESTORE mode. Assert that the position value is set but the motor does NOT move (if the controller chooses not to move in this mode).

6. **GuiLayout and ImProcess layout keys still work.**
   - The `GuiLayout` adapter in `ImConMainController` MUST register and save/load dock positions and splitter sizes.
   - GuiLayout MUST NOT appear in the setup-mode component list (it is STARTUP_RESTORE-only).
   - ImProcess's layout persistence (if it registers a component) MUST continue to work.

7. **Public API preserved.**
   - `getWidgetStatePersistence()` MUST return a singleton that supports the old API: `register(name, controller)`, `saveWidgetState(name, state_name)`, `loadWidgetState(name, state_name)`, `saveAllWidgetStates(state_name)`, `loadAllWidgetStates(state_name)`, `save_to_file(path)`, `load_from_file(path)`.
   - `SetupModeController.listSetupModes()`, `getSetupMode(name)`, `saveSetupMode(...)`, `loadSetupMode(...)`, `deleteSetupMode(...)` MUST all work with the unified registry under the hood.

### 7.2 Phase 2 Per-Controller Migration Checklist

Each Phase 2 task (2a–2h) MUST verify:

1. **Component implements `StatefulComponentMixin`** with all four methods: `getComponentState`, `applyComponentState`, `describeComponentState`, `getComponentStateHazards`.
2. **`stateSchemaVersion`, `componentName`, and `legacyStateNames` are set correctly.**
3. **Old state files load.** If the component previously used `getWidgetState` or `getSetupModeState`, an old saved file MUST load via `applyComponentState` without errors (warnings are acceptable for missing devices).
4. **STARTUP_RESTORE enforces safety.** Test that forbidden actions (enable laser, start scan, move motor) are skipped in STARTUP_RESTORE mode.
5. **SETUP_MODE_APPLY allows activations.** Test that the same state, when applied in SETUP_MODE_APPLY mode, DOES perform activations.
6. **Hazard detection works.** Test that `getComponentStateHazards` returns appropriate records for high-power lasers, missing devices, etc.
7. **Summary is human-readable.** Test that `describeComponentState` returns formatted strings matching the current setup-mode inspector output (for components that already have summarizers).

### 7.3 Phase 3 and Phase 4 Compatibility

- **Phase 3a:** After wiring Laser/Settings/SLM into the setup-mode UI, verify that the old setup-mode files (which did NOT include Laser/Settings/SLM state, because they didn't implement the mixin) still load without errors. The new components should appear as "not saved" in old modes.
- **Phase 3b:** If legacy preset storage is retired (e.g., stop writing to `setupInfo.laserPresets`), provide an explicit one-time import shim that reads old presets into the new format. Test the import shim against real user config files.
- **Phase 4:** After removing the old `getWidgetState`/`setWidgetState` and `SetupModeMixin` shims, verify that all controllers use only `StatefulComponentMixin` and that no old code paths remain.

---

## 8. Summary and Implementation Notes

### 8.1 What This Spec Defines

1. **Contract:** `StatefulComponentMixin` with `getComponentState`, `applyComponentState`, `describeComponentState`, `getComponentStateHazards`; `ComponentStateApplyMode` enum.
2. **Safety policy:** Exact forbidden actions for `STARTUP_RESTORE` vs `SETUP_MODE_APPLY`.
3. **Naming:** Canonical component names, legacy alias table, and resolution rules.
4. **Storage:** On-disk schema, legacy storage roots (preserved), and read strategy for all existing artifacts.
5. **Preview API:** Hazard record schema, context parameter schema, and mapping of existing summarizer logic to components.
6. **Legacy presets:** Policy (wrap and keep readable) and formats in scope.
7. **Compatibility:** Concrete checklist for Phase 1 tests.

### 8.2 What This Spec Does NOT Define

- The unified registry implementation details (Phase 1).
- The exact payload schemas for Laser, Settings, Scan, SLM, etc. (those are component-specific and remain flexible within a `stateSchemaVersion`).
- Storage-root consolidation plan (deferred to Phase 3c).
- Diff helper API (recommended but not required; can be a registry utility or component method).

### 8.3 How to Use This Spec

- **Phase 1 implementer:** Implement `StatefulComponentMixin`, `ComponentStateApplyMode`, and the unified registry exactly as specified. Make the registry bridge old `getWidgetState`/`setWidgetState` and `getSetupModeState`/`applySetupModeState` calls. Use the alias table from Section 3.2. Write tests asserting the checklist from Section 7.1.
- **Phase 2 component migrator:** Migrate one controller at a time. Set `componentName` and `legacyStateNames` per Section 3.2. Implement the four mixin methods. Lift summarizer logic per Section 5.4. Test per Section 7.2. Refer to this spec verbatim for method signatures, safety policy, and hazard schema.
- **Phase 3 consumer integrator:** Replace raw-payload parsing in `SetupModesController` with calls to `describeComponentState` and `getComponentStateHazards`. Use the context parameter schema from Section 5.2. Use the hazard record schema from Section 5.3.

### 8.4 Open Questions and Future Work

- **Diff API:** Should diffs be a registry-level utility, a component method, or both? Current recommendation: registry-level helper that calls `describeComponentState` on old and new states and returns a line diff. Defer to Phase 3a.
- **Positioner/Rotator activation:** Should STARTUP_RESTORE set position values without moving, or skip them entirely? Current spec: MAY set values, MUST NOT move. Each controller chooses. Document the choice in the component.
- **SLM HDF5 payloads:** Should large HDF5 configs be referenced by path in component state, or embedded as base64? Current spec: Reference by path; do NOT embed. Phase 2d will finalize.

---

**End of Specification**

---

## Appendix A: Code Citations

All canonical names, legacy keys, and apply order entries in Section 3.2 are grounded in the actual source code as of the commit preceding this spec. Citations:

- `LaserController.py:79`: `getWidgetStatePersistence().register('LaserController', self)`
- `SettingsController.py:97`: `getWidgetStatePersistence().register('SettingsController', self)`
- `ScanControllerBase.py:30`: `getWidgetStatePersistence().register('ScanController', self)`
- `ScanControllerAdvanced.py:83`: `getWidgetStatePersistence().register('ScanControllerAdvanced', self)`
- `ScanControllerMoNaLISA.py:55`: `getWidgetStatePersistence().register('ScanControllerMoNaLISA', self)`
- `ScanControllerPointScan.py:24`: `getWidgetStatePersistence().register('ScanControllerPointScan', self)`
- `PositionerController.py:81`: `getWidgetStatePersistence().register('PositionerController', self)`
- `RotatorController.py:36`: `getWidgetStatePersistence().register('RotatorController', self)`
- `RecordingController.py:81`: `getWidgetStatePersistence().register('RecordingController', self)`
- `BeadRecController.py:122`: `getWidgetStatePersistence().register('BeadRecController', self)`
- `ImConMainController.py:106`: `_GuiLayoutStateAdapter` registered as `'GuiLayout'`
- `SetupModeController.py:22-30`: `applyOrder = ['Settings', 'Scan', 'SLMs', 'SLM', 'LeicaStand', 'FlipMirror', 'Laser']`
- `basecontrollers.py:39-52`: `SetupModeMixin` definition (`getSetupModeState` at 48, `applySetupModeState` at 51)
- `basecontrollers.py:105,340,366`: `SuperScanController(SetupModeMixin, ...)` with `getSetupModeState` (340) / `applySetupModeState` (366)
- `FlipMirrorController.py:6`: `class FlipMirrorController(SetupModeMixin, ImConWidgetController)`
- `SetupModesController.py:441-1249`: Summarizer methods `_summarizeSavedDetectorState`, `_summarizeSavedLaserState`, `_summarizeSavedScanState`, `_summarizeSavedSLMState`, `_getHighPowerLaserEntries`, etc.

All line numbers are approximate and may shift; the source files are the authoritative reference.

---

**Document Metadata:**
- **Author:** Phase 0 specification task (ImSwitch2 state persistence unification)
- **Date:** 2026-06-18
- **Normative:** Yes. Phase 1 and Phase 2 implementations MUST conform to this spec.
- **Changelog:**
  - 2026-06-18: Initial version (Phase 0, Task 1 deliverable)

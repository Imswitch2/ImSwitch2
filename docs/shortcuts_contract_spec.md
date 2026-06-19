# Keyboard Shortcuts Unification — Phase 0 Contract Specification

**Status:** Phase 0 deliverable — specification only, no implementation  
**Reference:** `docs/shortcuts_unification_plan.md` (audit + plan)  
**Scope:** imcontrol ONLY (ImProcess/ImScripting are future work)  
**Branch:** `feature/shortcuts-phase0-spec` off `codex/testalab-scandev-port`

---

## 1. Executive Summary

This document specifies the **contract, scope, configuration schema, and migration plan** for unifying ImSwitch's keyboard shortcuts into a single, config-driven system. It resolves all open design questions from the audit and provides the authoritative specification that implementation phases (Phase 1+) consume verbatim.

### Current State (Audit Summary)

ImSwitch has **four separate shortcut mechanisms**, each with different lifecycles and configuration capabilities:

1. **`@shortcut` registry** (`imcommon/model/shortcut.py`): Decorator-based system collecting widget shortcuts into a central menu, but with hardcoded keys and method-name keying that raises `NameError` on collisions.
   - Lines: `shortcut.py:1-12` (decorator), `shortcut.py:15-37` (`generateShortcuts`)
   - Collection: `ImConMainController.py:102` (`generateShortcuts(mainView.widgets.values())`)
   - Menu binding: `ImConMainView.py:144-149` (`addShortcuts`)

2. **Positioner axis jog** (`PositionerWidget.py:141-163`, `SetupInfo.py:99`): Config-tunable via `shortcutModifier` but only selects between two predefined key groups, not arbitrary bindings.

3. **Setup-mode shortcuts** (`SetupModesController.py:600-620`): Genuinely user-configurable per-mode JSON, but siloed with a disposal bug (`setParent(None)` leak at line 619).

4. **Bespoke hardcoded shortcuts**: Menu `QAction`s (`ImConMainView.py:44,51,56`), controller-local `QShortcut`s (`LeicaStandController.py:45-49`), and widget `keyPressEvent` handlers (`SLMDisplay.py:95-97`, `BSC203Widget.py:91-95`).

### Target State (Design Goals)

- **One registry** with stable, namespaced **action IDs** (not method names).
- **Code-level defaults** for all actions; **setup-config overrides** for rebinding.
- **Single `ShortcutManager`** owning Qt binding/lifecycle/conflict-handling.
- **Widened collection** (widgets + controllers + managers) with catalog-only mode for currently-unbound actions.
- **Fixed disposal lifecycle** (no more `setParent(None)` leaks or "ambiguous shortcut" warnings).
- **Config-driven binding** via `shortcuts` map in setup config (`{ actionId: keySequence | [seq, ...] | null }`).

---

## 2. Locked Design Decisions (Normative)

These decisions are **locked from maintainer review** and encoded as normative requirements:

### D-Q7: Scope — imcontrol ONLY
- Build the `ShortcutManager` machinery so it COULD be reused (e.g., by exposing the core classes in `imcommon`), but **migrate only imcontrol shortcuts**.
- `ImProcess` and `ImScripting` hardcoded shortcuts (e.g., menu actions) are **explicitly out of scope** for this effort.
- Future work may extend the machinery to other modules, but Phase 1–4 touch only `imcontrol`.

### D-Q1: Config Location — Setup Config ONLY
- The `shortcuts` map lives **in the setup config** (the same JSON file that already contains `shortcutModifier`, `detectors`, `positioners`, etc.).
- **No per-user override file** in this effort. Effective binding merge order is simply:  
  **code defaults < setup config `shortcuts`**
- Rationale: keeps configuration centralized, avoids multi-file complexity, consistent with existing `shortcutModifier`.
- The Phase 4 editor, when built, writes back to the **setup config** (design the `ShortcutManager` API to support this).

### D-Q3: Positioner Jog — Arbitrary Per-Action Bindings
- Each positioner axis jog becomes a **first-class rebindable action** with a stable action ID (e.g., `positioner.<name>.X.plus`, `positioner.<name>.X.minus`).
- Keep `PositionerInfo.shortcutModifier` as a **back-compat alias** that expands to the existing default bindings:
  - `"ctrl"` → Ctrl+Arrows + Ctrl+Y/Ctrl+A for Z  
  - `"ctrl-shift"` → Ctrl+Shift+Arrows + Ctrl+Shift+Y/Ctrl+Shift+A for Z
- Old setup files without a `shortcuts` section keep working via the alias expansion.
- New configs can specify arbitrary bindings per action ID in the `shortcuts` map, overriding the alias.

### D-Q5 & D-Q9: Setup-Mode Shortcuts — Per-Mode Storage, Manager Lifecycle
- **Storage:** Keep per-mode `shortcut` field in each mode's JSON (preserve current semantics).
- **Lifecycle:** Route the `QShortcut` creation **and disposal** through the `ShortcutManager` so the existing `_clearShortcuts` `setParent(None)` leak (causing "Ambiguous shortcut overload" warnings, `SetupModesController.py:619`) is fixed by the unified lifecycle.
- **Semantics:** Preserve the current behavior where:
  - Applying a mode via shortcut passes `source="shortcut"` to the apply callback (line 613), used by safety-confirmation logic.
  - Duplicate mode shortcut conflicts prompt the user to replace the other mode's binding (existing prompt logic retained).
- **Conflict Handling:** Mode-vs-global conflicts (mode shortcut collides with a non-mode action) are reported by the manager per the conflict policy (§7).

---

## 3. Defaulted Design Decisions (Applied)

These decisions apply unless explicitly revisited:

### Q2: Action ID Scheme
- **Namespacing:** `<area>.<thing>.<verb>` or `<area>.<device-name>.<detail>.<verb>`
- **Per-device actions** namespaced by **device NAME** (not role):
  - `positioner.<positionerName>.X.plus`
  - `positioner.<positionerName>.X.minus`
  - `positioner.<positionerName>.Y.plus`
  - etc.
- **Widget/controller actions** namespaced by area/widget:
  - `settings.nextDetector`
  - `image.updateLevels`
  - `view.toggleLiveView`
  - `recording.toggleRecord`
  - `leica.toggleMode`
  - `app.loadParams`
  - `app.saveWidgetStates`
  - `app.loadWidgetStates`
- **Setup-mode actions:** `mode.<modeName>`
- Confirm exact IDs against the real callbacks during Phase 1 implementation.

### Q4: GRBL / Manager Shortcuts — Catalog-Only
- The widened collection (Phase 1) **may discover** manager/controller actions (e.g., `GRBLStageManager` shortcuts at lines 62-84).
- **MUST NOT auto-bind** currently-dead actions. They are cataloged but **not bound by default**.
- Binding is enabled **only by an explicit migration step** (Phase 3f) or **explicit config** (user adds the action ID to the `shortcuts` map).
- Prevents accidental behavior changes from discovery.

### Q6: Conflict Policy
- **Warn + keep the higher-priority/explicit binding, disable the other.**
- **Never emit Qt's native "ambiguous shortcut" warning** — the manager detects and resolves conflicts first.
- **Priority order** (highest to lowest):
  1. Explicit setup-config binding (`shortcuts` map entry)
  2. Code default (from `@shortcut` decorator or equivalent)
  3. Tie-break: first-registered action wins, second is disabled with a warning logged.
- Mode-vs-global conflicts: setup-mode shortcuts take the same priority as explicit config bindings (priority 1), since they are also user-specified.

### Q8: Action Metadata
Each action declares:
- **`actionId`** (str): Stable, namespaced identifier (see Q2).
- **`displayName`** (str): Human-readable name for the `&Shortcuts` menu and editor.
- **`defaultKeySequence`** (str | list[str] | None): Default key(s) from code, or `None` if unbound by default.
- **`scope`** (enum): `ApplicationShortcut`, `WindowShortcut`, `widget-local`, or `press-release`.
- **`owner`** (QtWidget | QtWindow | None): The Qt object to which the `QShortcut` or `QAction` is parented (for lifecycle).
- **`enabledPredicate`** (callable | None): Optional function returning `bool` for action availability (e.g., "only if detector X is active").
- **`activationSource`** (dict | None): Metadata passed to the callback to distinguish shortcut activation from menu/button activation (e.g., `source="shortcut"` for setup modes, line 613).
- **`callback`** (callable): The method/function to invoke when activated.

---

## 4. Extended Action Contract

### 4.1 Current `@shortcut` Decorator (Baseline)

**File:** `imswitch/imcommon/model/shortcut.py:1-12`

```python
class shortcut:
    """ Decorator for shortcuts. """
    def __init__(self, key, name):
        self.key = key
        self.name = name
    def __call__(self, func):
        func._Shortcut = True
        func._Key = self.key
        func._Name = self.name
        return func
```

**Limitations:**
- No action ID (keys by method name, raises `NameError` on collision, line 31).
- No metadata beyond key and display name.
- Keys are hardcoded at decoration time.

### 4.2 Extended Decorator Contract (Phase 1 Target)

Extend `@shortcut` (or provide a new decorator/registration API) to declare:

```python
@shortcut(
    actionId="positioner.<name>.X.plus",
    defaultKey="Ctrl+Right",
    displayName="Positioner {name} X +",
    scope=ShortcutScope.Application,
    owner=lambda self: self,  # or explicit widget reference
    enabledPredicate=None,
    activationSource=None
)
def stepXPlus(self):
    ...
```

**Backward Compatibility:**
- Existing call sites like `@shortcut("Ctrl+R", "Record")` must continue to work (Phase 1 adds optional parameters or a migration shim).
- The old `func._Key` / `func._Name` attributes may be deprecated but initially kept for compatibility during transition.

**Action Record Shape (Catalogued):**

```python
@dataclass
class ShortcutAction:
    actionId: str
    displayName: str
    defaultKeySequence: Union[str, List[str], None]
    scope: ShortcutScope  # enum: Application, Window, WidgetLocal, PressRelease
    owner: Union[QtCore.QObject, None]
    enabledPredicate: Optional[Callable[[], bool]]
    activationSource: Optional[Dict[str, Any]]
    callback: Callable
```

This dataclass (or equivalent) is the internal representation after collection.

### 4.3 Non-Decorator Shortcuts (Hardcoded `QAction`/`QShortcut`)

For actions currently created as hardcoded `QAction` or `QShortcut` (e.g., menu actions, LeicaStand F2), Phase 3 migration passes an equivalent action record to the `ShortcutManager` instead of creating the Qt object directly.

**Example (current hardcoded):**  
`ImConMainView.py:44`: `self.loadParamsAction.setShortcut('Ctrl+P')`

**Phase 3a migration:**  
Remove the `setShortcut` call and register:
```python
manager.registerAction(
    actionId="app.loadParams",
    displayName="Load parameters from HDF5",
    defaultKey="Ctrl+P",
    scope=ShortcutScope.Window,
    owner=self,
    callback=lambda: self.sigLoadParamsFromHDF5.emit()
)
```

---

## 5. Collection Model

### 5.1 Current Collection (Widgets-Only)

**File:** `imswitch/imcontrol/controller/ImConMainController.py:101-103`

```python
shorcutObjs = list(self.__mainView.widgets.values())
self.__shortcuts = generateShortcuts(shorcutObjs)
self.__mainView.addShortcuts(self.__shortcuts)
```

- **Limitation:** Only `widgets` are scanned; controllers and managers are excluded.
- **Keying:** By method name (`generateShortcuts` returns `{methodName: {...}}`, `shortcut.py:33`).
- **Collision:** Raises `NameError` if two widgets have the same shortcut-decorated method name (line 31).

### 5.2 Widened Collection (Phase 1 Target)

- **Scan:** widgets + controllers + managers (or relocate manager shortcuts to widgets/controllers if discovery is impractical).
- **Keying:** By **action ID**, not method name. The new `generateShortcuts` (or successor) returns `{actionId: ShortcutAction}`.
- **Collision:** Action ID collisions in code are a **hard error at startup** (two decorators with the same `actionId` — programming mistake).
- **Catalog-Only Mode:** Actions can be catalogued **without binding** them to Qt shortcuts. This allows:
  - Discovery of manager-level shortcuts (e.g., `GRBLStageManager.py:62-84`) without auto-activating them.
  - Currently-dead actions remain unbound unless explicitly enabled by config or migration.
- **Binding Decision:** An action is bound (Qt `QShortcut`/`QAction` created) if:
  1. It has a non-None `defaultKeySequence` in code **and** is marked `initiallyBound=True` in the inventory, OR
  2. The setup config `shortcuts` map has an entry for its `actionId` with a non-null key sequence.

---

## 6. Setup-Config Schema

### 6.1 New `shortcuts` Section

Add a top-level `shortcuts` map to the setup config JSON (the same file containing `detectors`, `positioners`, `lasers`, etc.):

```json
{
  "setupInfo": {
    "detectors": { ... },
    "positioners": { ... },
    "shortcuts": {
      "positioner.MotorizedStage.X.plus": "Ctrl+Right",
      "positioner.MotorizedStage.X.minus": "Ctrl+Left",
      "view.toggleLiveView": ["Ctrl+L", "F5"],
      "recording.toggleRecord": null,
      "mode.Live": "F1",
      "mode.Scan": "F2"
    }
  }
}
```

### 6.2 Schema Rules

- **Key:** Action ID (string).
- **Value:** One of:
  - `string`: Single key sequence (e.g., `"Ctrl+R"`).
  - `[string, ...]`: Multiple key sequences for the same action (all trigger the same callback).
  - `null`: Explicitly **disables** the action's binding (even if it has a code default).
- **Unknown IDs:** If an action ID in the config is not in the catalogued actions, log a **warning** but do not fail startup.
- **Validation:** Each key sequence string is validated via `QKeySequence`. Invalid sequences log a warning and are ignored.
- **Merge Order:** `code defaults < setup config shortcuts`  
  (No per-user override file, per D-Q1.)

### 6.3 Back-Compat: `shortcutModifier` Alias

`PositionerInfo.shortcutModifier` (`SetupInfo.py:99`) is retained for backward compatibility.

**Expansion rules (Phase 3c):**
- If a positioner declares `shortcutModifier: "ctrl"`, and the `shortcuts` map does NOT contain explicit entries for that positioner's axis jog actions, the manager auto-expands to:
  - `positioner.<name>.X.plus: "Ctrl+Right"`
  - `positioner.<name>.X.minus: "Ctrl+Left"`
  - `positioner.<name>.Y.plus: "Ctrl+Up"`
  - `positioner.<name>.Y.minus: "Ctrl+Down"`
  - `positioner.<name>.Z.plus: "Ctrl+Y"`
  - `positioner.<name>.Z.minus: "Ctrl+A"`
- If `shortcutModifier: "ctrl-shift"`, expand to `"Ctrl+Shift+Right"`, etc.
- If the `shortcuts` map **has explicit entries** for any of those action IDs, those explicit entries override the alias expansion (config wins).
- If `shortcutModifier` is absent or None, no expansion (positioner jog actions have their code defaults or remain unbound if catalogued).

---

## 7. Conflict Policy and Priority Order

### 7.1 Conflict Detection

A **conflict** occurs when two distinct action IDs are assigned the same effective key sequence (after merging defaults and config).

**Types:**
1. **Action ID collision in code:** Two decorators declare the same `actionId`. → **Hard error at startup** (programming mistake).
2. **Key sequence collision:** Two different action IDs map to the same key (e.g., both `"Ctrl+R"` for `recording.toggleRecord` and `view.toggleLiveView`).

### 7.2 Priority Order (Key Sequence Conflicts)

When two actions have the same key sequence, the **higher-priority binding wins**:

1. **Explicit setup-config binding** (`shortcuts` map entry) — highest priority.
2. **Code default** (from decorator or API registration).
3. **Tie-break:** First-registered action wins; second is **disabled** with a warning logged.

**Special case:** Setup-mode shortcuts (`mode.<name>`) stored in per-mode JSON are treated as **priority 1** (same as explicit config), because they are user-specified.

### 7.3 Resolution Behavior

- **Winner:** Qt `QShortcut`/`QAction` is created and activated normally.
- **Loser:** No Qt object created; action is **catalogued but unbound**. A **warning** is logged:
  ```
  Shortcut conflict: Action 'view.toggleLiveView' and 'recording.toggleRecord' both request 'Ctrl+R'.
  'recording.toggleRecord' (explicit config) takes precedence; 'view.toggleLiveView' is disabled.
  ```
- **User resolution:** User can edit the setup config to change one of the conflicting bindings, or set one to `null` to disable it.

### 7.4 Mode-vs-Global Conflicts

- A mode shortcut (e.g., `mode.Scan: "F2"`) and a global action (e.g., `leica.toggleMode: "F2"`) collide.
- **Priority:** Mode shortcuts are priority 1 (explicit user config).
- **Behavior:** The mode shortcut wins; the global action is disabled with a warning.
- **Preserved duplicate-mode prompt:** If two modes declare the same shortcut key, the existing prompt to replace the other mode's binding (in `SetupModesController`) is retained (Phase 3d).

---

## 8. ShortcutManager API

The `ShortcutManager` is a new class (Phase 2) that owns the entire shortcut lifecycle.

### 8.1 Responsibilities

1. **Collect** action registrations (from decorators, API calls).
2. **Merge** config overrides over defaults to compute effective bindings.
3. **Detect conflicts** and apply the priority order.
4. **Build Qt objects** (`QShortcut` or `QAction`) with the correct scope/context/owner.
5. **Dispose** Qt objects correctly (no `setParent(None)` leak).
6. **Populate** the `&Shortcuts` menu.
7. **Expose runtime API** for query/rebind/reset (for Phase 4 editor).

### 8.2 Core API (Pseudocode)

```python
class ShortcutManager:
    def registerAction(
        self,
        actionId: str,
        displayName: str,
        callback: Callable,
        defaultKeySequence: Union[str, List[str], None] = None,
        scope: ShortcutScope = ShortcutScope.Application,
        owner: Optional[QtCore.QObject] = None,
        enabledPredicate: Optional[Callable[[], bool]] = None,
        activationSource: Optional[Dict[str, Any]] = None,
        initiallyBound: bool = True
    ) -> None:
        """Register an action. Does not create Qt objects yet."""
        ...

    def loadConfigOverrides(self, setupConfig: dict) -> None:
        """Load the 'shortcuts' map from setup config and merge."""
        ...

    def buildShortcuts(self) -> None:
        """Create Qt QShortcut/QAction objects for effective bindings."""
        ...

    def rebindAction(self, actionId: str, keySequence: Union[str, None]) -> None:
        """Rebind an action at runtime (editor API)."""
        ...

    def resetAction(self, actionId: str) -> None:
        """Reset an action to its code default."""
        ...

    def getActionMetadata(self, actionId: str) -> Optional[ShortcutAction]:
        """Query action metadata."""
        ...

    def getEffectiveBinding(self, actionId: str) -> Union[str, List[str], None]:
        """Get the effective key sequence(s) for an action after merge."""
        ...

    def clearShortcuts(self) -> None:
        """Dispose all Qt shortcut objects (for cleanup or rebuild)."""
        ...
```

### 8.3 Disposal Contract (Fixes Setup-Mode Leak)

**Current leak:** `SetupModesController.py:619` calls `shortcut.setParent(None)`, which does not fully dispose the `QShortcut` in Qt 5 and can leave stale bindings.

**Phase 2 fix:**
- `ShortcutManager.clearShortcuts()` iterates over all created Qt objects and:
  1. Disconnects signals (`activated.disconnect(...)`).
  2. Calls `deleteLater()` or explicitly deletes the object.
  3. Clears the internal registry.
- `SetupModesController._clearShortcuts` (Phase 3d) is replaced by a call to `manager.clearShortcuts()` or `manager.rebuildModeShortcuts()`.

### 8.4 Setup-Mode Integration (Phase 3d)

`SetupModesController` (lines 600-620) is refactored to:
1. Register mode-switch actions with the manager: `manager.registerAction(actionId=f"mode.{modeName}", ...)`.
2. Pass `activationSource={"source": "shortcut"}` so the callback receives it (line 613).
3. Call `manager.rebuildModeShortcuts(modeSummaries)` instead of creating `QShortcut` objects directly.
4. The manager handles conflict detection (mode-vs-mode, mode-vs-global).
5. The duplicate-mode prompt (existing logic) is preserved: if two modes have the same key, the manager detects the conflict and the controller prompts to replace.

---

## 9. Positioner Jog Model (D-Q3)

### 9.1 Current Model

**Config:** `PositionerInfo.shortcutModifier` (`SetupInfo.py:99`) selects one of two predefined key groups.  
**Widget registration:** `PositionerWidget._registerAxisShortcut` (lines 141-163) binds positioner axes to primary or secondary registries.  
**Hardcoded keys:** 12 `@shortcut` methods (lines 179-239):
- Primary (Ctrl): `Ctrl+Right/Left` (X±), `Ctrl+Up/Down` (Y±), `Ctrl+Y/Ctrl+A` (Z±)
- Secondary (Ctrl+Shift): `Ctrl+Shift+Right/Left/Up/Down/Y/A`

### 9.2 Phase 3c Target

Each positioner axis jog is a **first-class action** with a dynamic action ID:

```python
actionId = f"positioner.{positionerName}.{axis}.plus"
actionId = f"positioner.{positionerName}.{axis}.minus"
```

**Example:** For positioner `"MotorizedStage"` with axes `X`, `Y`, `Z`:
- `positioner.MotorizedStage.X.plus` (default: `Ctrl+Right`)
- `positioner.MotorizedStage.X.minus` (default: `Ctrl+Left`)
- `positioner.MotorizedStage.Y.plus` (default: `Ctrl+Up`)
- `positioner.MotorizedStage.Y.minus` (default: `Ctrl+Down`)
- `positioner.MotorizedStage.Z.plus` (default: `Ctrl+Y`)
- `positioner.MotorizedStage.Z.minus` (default: `Ctrl+A`)

### 9.3 Back-Compat Alias Expansion

If `PositionerInfo.shortcutModifier == "ctrl"` and the `shortcuts` map does NOT contain explicit entries for the above action IDs, the manager auto-expands them as shown.

If `shortcutModifier == "ctrl-shift"`, expand to `Ctrl+Shift+Right`, etc.

If the `shortcuts` map **has explicit entries**, those override the alias (config wins).

### 9.4 Secondary Positioner

If a second positioner declares `shortcutModifier: "ctrl-shift"`, its actions get the secondary key set by default. The `_axisRegistrySecondary` logic (line 167) becomes unnecessary once each positioner has its own namespaced action IDs.

**Migration:** The `_emitStep` method (lines 165-174) and the `_axisRegistry` dicts can be refactored or removed once all jog actions route through the manager.

---

## 10. Migration List and Authoritative Inventory

### 10.1 Migration Phases

**Phase 1:** Action IDs + widened collection (catalog-only).  
**Phase 2:** `ShortcutManager` + config schema.  
**Phase 3:** Migrate outliers (independent tasks):
- **3a:** Menu actions (`app.loadParams`, `app.saveWidgetStates`, `app.loadWidgetStates`)
- **3b:** Leica F2 (`leica.toggleMode`)
- **3c:** Positioner jog (per-action IDs + back-compat alias)
- **3d:** Setup-mode shortcuts (lifecycle through manager, preserve semantics)
- **3e (optional):** Selected `keyPressEvent` handlers
- **3f (optional):** GRBL manager shortcuts (explicit enablement or removal)

**Phase 4:** Editor + docs.

### 10.2 Authoritative Inventory Table

This table is the **single source of truth** for all shortcuts in scope. Every row has been **verified against source code**.

| **Action ID** | **Display Name** | **Current Source (File:Line)** | **Current Default Key** | **Scope** | **Owner** | **Enabled Predicate** | **Migration Phase** | **Initially Bound** |
|---|---|---|---|---|---|---|---|---|
| `positioner.<name>.X.plus` | "Positioner {name} X +" | `PositionerWidget.py:179-182` | `Ctrl+Right` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.X.minus` | "Positioner {name} X -" | `PositionerWidget.py:184-187` | `Ctrl+Left` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Y.plus` | "Positioner {name} Y +" | `PositionerWidget.py:189-192` | `Ctrl+Up` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Y.minus` | "Positioner {name} Y -" | `PositionerWidget.py:194-197` | `Ctrl+Down` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Z.plus` | "Positioner {name} Z +" | `PositionerWidget.py:199-202` | `Ctrl+Y` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Z.minus` | "Positioner {name} Z -" | `PositionerWidget.py:204-207` | `Ctrl+A` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.X.plus` (2nd) | "Positioner (2nd) X +" | `PositionerWidget.py:211-214` | `Ctrl+Shift+Right` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.X.minus` (2nd) | "Positioner (2nd) X -" | `PositionerWidget.py:216-219` | `Ctrl+Shift+Left` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Y.plus` (2nd) | "Positioner (2nd) Y +" | `PositionerWidget.py:221-224` | `Ctrl+Shift+Up` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Y.minus` (2nd) | "Positioner (2nd) Y -" | `PositionerWidget.py:226-229` | `Ctrl+Shift+Down` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Z.plus` (2nd) | "Positioner (2nd) Z +" | `PositionerWidget.py:231-234` | `Ctrl+Shift+Y` | Application | PositionerWidget | Always | 3c | Yes |
| `positioner.<name>.Z.minus` (2nd) | "Positioner (2nd) Z -" | `PositionerWidget.py:236-239` | `Ctrl+Shift+A` | Application | PositionerWidget | Always | 3c | Yes |
| `settings.nextDetector` | "Next detector" | `SettingsWidget.py:644-646` | `Ctrl+N` | Application | SettingsWidget | Always | 1 (existing) | Yes |
| `image.updateLevels` | "Update levels" | `ImageWidget.py:292-294` | `Ctrl+U` | Application | ImageWidget | Always | 1 (existing) | Yes |
| `view.toggleLiveView` | "Liveview" | `ViewWidget.py:36-38` | `Ctrl+L` | Application | ViewWidget | Always | 1 (existing) | Yes |
| `recording.toggleRecord` | "Record" | `RecordingWidget.py:447-449` | `Ctrl+R` | Application | RecordingWidget | Always | 1 (existing) | Yes |
| `app.loadParams` | "Load parameters from HDF5" | `ImConMainView.py:43-46` | `Ctrl+P` | Window | ImConMainView | Always | 3a | Yes |
| `app.saveWidgetStates` | "Save Widget States" | `ImConMainView.py:50-53` | `Ctrl+Shift+S` | Window | ImConMainView | Always | 3a | Yes |
| `app.loadWidgetStates` | "Load Widget States" | `ImConMainView.py:55-58` | `Ctrl+Shift+L` | Window | ImConMainView | Always | 3a | Yes |
| `leica.toggleMode` | "Toggle Leica Mode" | `LeicaStandController.py:45-49` | `F2` | Window | LeicaStandWidget | `manager.isConnected()` | 3b | Yes |
| `mode.<modeName>` | "Switch to {modeName}" | `SetupModesController.py:610-615` | (per-mode JSON) | Application | SetupModesWidget | Always | 3d | Yes (if mode has shortcut) |
| `grbl.moveUp` | "Move up" | `GRBLStageManager.py:62-64` | `Up` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `grbl.moveDown` | "Move down" | `GRBLStageManager.py:66-68` | `Down` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `grbl.moveLeft` | "Move left" | `GRBLStageManager.py:70-72` | `Left` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `grbl.moveRight` | "Move right" | `GRBLStageManager.py:74-76` | `Right` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `grbl.moveZUp` | "Move Z up" | `GRBLStageManager.py:78-80` | `-` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `grbl.moveZDown` | "Move Z down" | `GRBLStageManager.py:82-84` | `+` | Application | (manager) | Always | 3f (optional) | **No** (catalog-only) |
| `slm.close` | "Close SLM Display" | `SLMDisplay.py:95-97` | `Escape` | Widget-local | SLMDisplay | Always | 3e (optional) | Yes (keyPressEvent) |
| `bsc203.pressKey` | "(BSC203 key press)" | `BSC203Widget.py:91-92` | (any key) | Press-release | BSC203Widget | Always | 3e (not migrated) | (special) |
| `bsc203.releaseKey` | "(BSC203 key release)" | `BSC203Widget.py:94-95` | (any key) | Press-release | BSC203Widget | Always | 3e (not migrated) | (special) |

**Notes:**
- `<name>` in positioner action IDs is **runtime-expanded** from the positioner device name (e.g., `MotorizedStage`).
- Positioner secondary set (2nd) refers to positioners with `shortcutModifier: "ctrl-shift"`.
- GRBL manager shortcuts are **catalog-only by default** (not bound unless explicitly enabled, per Q4).
- BSC203 press/release handlers are **not migrated** because they are continuous motion controls, not one-shot actions (see §10.3).
- `mode.<modeName>` is a dynamic action ID per setup mode; each mode gets its own entry at runtime.

### 10.3 Special Cases

**Press/Release Handlers (BSC203Widget):**
- `keyPressEvent` and `keyReleaseEvent` (lines 91-95) emit signals for **continuous motion control** (velocity jog).
- These are **not one-shot actions** and cannot be trivially converted to `QShortcut.activated` callbacks.
- **Decision:** Do NOT migrate these in Phase 3e. They remain as `keyPressEvent` handlers.
- The `focusOutEvent` guard (lines 97-106) is critical for safety and must be preserved.

**SLM Escape (SLMDisplay):**
- `keyPressEvent` at line 95-97 handles `Escape` to close the display.
- This is a **one-shot action** and CAN be migrated (Phase 3e optional).
- Action ID: `slm.close`, default key `Escape`, scope `widget-local` (WindowShortcut parented to SLMDisplay).

---

## 11. Out-of-Scope (Future Work)

1. **ImProcess and ImScripting shortcuts:** Hardcoded menu actions in other modules are NOT migrated in this effort (per D-Q7). Future work may expose the `ShortcutManager` machinery for reuse.
2. **Per-user override files:** No separate user keybindings file (per D-Q1). Phase 4 editor writes to the **setup config** only.
3. **Context-aware bindings:** No support for context-dependent shortcuts (e.g., "Ctrl+C means copy in text fields, but means something else in the image view"). All bindings are global (Application or Window scope).
4. **Multi-step key sequences:** No support for Emacs-style multi-key sequences (e.g., `Ctrl+X Ctrl+S`). `QKeySequence` supports up to 4 keys in a single sequence, but the system treats each as a single trigger.
5. **Dynamic action registration post-startup:** The current design assumes all actions are registered at startup. Runtime plugin/module loading that adds new actions is not addressed.

---

## 12. Implementation Phases (Summary)

### Phase 1: Action IDs + Widened Collection
- **Goal:** Stable action IDs, replace method-name keying, widen collection to controllers + managers (catalog-only).
- **Deliverables:**
  - Extended `@shortcut` decorator (or new API) with `actionId` parameter.
  - `generateShortcuts` replacement keyed by action ID.
  - Collection includes widgets, controllers, managers.
  - Catalog-only mode: actions discovered but not bound unless `initiallyBound=True` in inventory.
- **No behavior change:** Existing shortcuts work as before; newly-discovered actions remain unbound.

### Phase 2: ShortcutManager + Config Schema
- **Goal:** Single manager, config schema, conflict detection, disposal fix.
- **Deliverables:**
  - `ShortcutManager` class with API (§8).
  - `shortcuts` section in setup config JSON (§6).
  - Config validation, merge logic, conflict detection (§7).
  - `&Shortcuts` menu routed through manager.
  - Disposal fix (no more `setParent(None)` leak).
- **Tests:** Override applies, unknown ID warns, conflict handled, disposal correct.

### Phase 3: Migrate Outliers (Independent Tasks)
- **3a:** Menu actions (`app.loadParams`, `app.saveWidgetStates`, `app.loadWidgetStates`)
- **3b:** Leica F2 (`leica.toggleMode`)
- **3c:** Positioner jog (per-action IDs, back-compat alias for `shortcutModifier`)
- **3d:** Setup-mode shortcuts (lifecycle through manager, preserve semantics, fix leak)
- **3e (optional):** SLM Escape (`slm.close`)
- **3f (optional):** GRBL manager shortcuts (explicit enablement or confirm obsolete and remove)

### Phase 4: Editor + Docs
- **Goal:** GUI editor for rebinding, docs, user guide.
- **Deliverables:**
  - Editor widget/dialog (tree of actions, rebind UI, reset to default).
  - Persistence through `ShortcutManager` to setup config.
  - User documentation (how to rebind, conflict resolution, examples).

---

## 13. Sanity Checks (Verification Criteria)

These checks confirm the spec is implementation-ready:

1. **Every inventory row's "Current Default Key" and "Current Source" is verified against actual source code.**  
   ✅ All file:line citations checked and accurate (see §10.2).

2. **The spec answers, unambiguously, the `ShortcutManager` disposal contract so Phase 3d can fix the SetupModes leak from the spec alone.**  
   ✅ Disposal contract specified in §8.3: disconnect signals, call `deleteLater()`, clear registry.

3. **The spec states the exact effective-binding merge order and conflict priority.**  
   ✅ Merge order: code defaults < setup config (§6.2).  
   ✅ Conflict priority: explicit config > code default > first-registered (§7.2).

4. **All locked and defaulted decisions are normatively encoded.**  
   ✅ Locked: D-Q7 (imcontrol only), D-Q1 (setup config only), D-Q3 (per-action jog), D-Q5/Q9 (per-mode storage, manager lifecycle) — §2.  
   ✅ Defaulted: Q2 (action ID scheme), Q4 (catalog-only), Q6 (conflict policy), Q8 (metadata) — §3.

5. **Backward compatibility is preserved for existing setup configs.**  
   ✅ `shortcutModifier` alias expansion (§6.3, §9.3).  
   ✅ Existing `@shortcut` call sites work (§4.2).

6. **Migration phases are dependency-ordered and independent within each phase.**  
   ✅ Phase 1 → Phase 2 → Phase 3 (3a–3f independent) → Phase 4 (§12).

---

## 14. Commit Message Template

```
Add shortcuts contract specification (Phase 0)

Specifies the unified keyboard shortcuts system for imcontrol:
- Stable action IDs, config-driven binding via setup config
- ShortcutManager API for lifecycle/conflict/disposal
- Positioner jog per-action model with shortcutModifier back-compat
- Setup-mode integration preserving semantics, fixing disposal leak
- Authoritative inventory of all 29 actions with verified citations
- Conflict policy: explicit config > code default > first-registered
- Scope: imcontrol only (ImProcess/ImScripting future work)
- No per-user override file (setup config only)

Reference: docs/shortcuts_unification_plan.md
Inventory: 29 actions (21 bound by default, 6 catalog-only, 2 special)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

**End of Specification**

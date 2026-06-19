# Keyboard shortcuts unification — audit and plan

Goal: move from today's mostly compile-time, per-widget/per-device shortcut
bindings to a single, config-driven system where any action can be bound to any
key sequence through the setup config (with sensible code-level defaults).

Audit targets:
- `imswitch/imcommon/model/shortcut.py` (`@shortcut` decorator + `generateShortcuts`)
- `imswitch/imcontrol/controller/ImConMainController.py` (shortcut collection)
- `imswitch/imcontrol/view/ImConMainView.py` (`&Shortcuts` menu, hardcoded menu actions)
- `imswitch/imcontrol/view/widgets/PositionerWidget.py` (axis jog binding)
- `imswitch/imcontrol/model/SetupInfo.py` (`shortcutModifier` config field)
- `imswitch/imcontrol/controller/controllers/SetupModesController.py` (per-mode shortcuts)
- `imswitch/imcontrol/controller/controllers/LeicaStandController.py` (F2)
- `imswitch/imcontrol/model/managers/positioners/GRBLStageManager.py` (manager-level `@shortcut`)

---

## 1. Audit findings

There are **four separate shortcut mechanisms**, only loosely related. Almost
all bindings are fixed at source-compile time; very little is config-driven.

### 1.1 The `@shortcut` registry — a half-built central system (the foundation)

- `@shortcut(key, name)` decorator (`imcommon/model/shortcut.py`) tags a method
  with `_Shortcut/_Key/_Name`. `generateShortcuts(objs)` scans an object list and
  returns `{methodName: {callback, key, name}}`.
- `ImConMainController` builds the registry from `self.__mainView.widgets.values()`
  only, then `ImConMainView.addShortcuts(...)` creates one `QAction` per entry
  (with `setShortcut(key)`) under the **`&Shortcuts` menu**.
- Limitations:
  - **Keys are hardcoded in the decorators** — not configurable.
  - **Only widgets are scanned**, not controllers or managers.
  - `generateShortcuts` keys the dict by **method name**, and raises `NameError`
    on duplicate method names across widgets — a fragile global namespace.
- Currently decorated: `PositionerWidget` (Ctrl+Arrows, Ctrl+Shift+Arrows,
  Ctrl+Y/Ctrl+A for Z), `SettingsWidget` (Ctrl+N, next detector), `ImageWidget`
  (Ctrl+U, update levels).
- **Dead bindings:** `GRBLStageManager` declares `@shortcut` methods (arrows,
  `+`/`-`) but managers are NOT in `mainView.widgets`, so they are never
  collected. (Confirm intent — see open question Q4.)

### 1.2 Positioner axis jog — the only config-tunable piece, but not arbitrary

- `PositionerInfo.shortcutModifier` (`SetupInfo.py`, `Optional[str]`) selects
  between **two predefined key groups** in
  `PositionerWidget._registerAxisShortcut`:
  - `"ctrl"` → the Ctrl+Arrow set,
  - `"ctrl-shift"` → the Ctrl+Shift+Arrow set,
  - else → legacy first-come claim of the Ctrl+Arrow set.
- So config chooses *which predefined group* a positioner's axes map to and the
  axis→key mapping; the **actual key combos remain hardcoded** in the
  `@shortcut` methods. This is selection-among-fixed-groups, not arbitrary
  binding.

### 1.3 Setup-mode shortcuts — genuinely user-configurable, but siloed

- Each setup mode stores an optional `shortcut` in its mode JSON; bound at
  runtime as an `ApplicationShortcut` `QShortcut` in `SetupModesController`.
- This is the only place a user freely assigns a key — but only for
  mode-switching, and it currently has a disposal bug: `_clearShortcuts` uses
  `setParent(None)` instead of disposing the `QShortcut`, which can leave stale
  shortcuts registered and produce "Ambiguous shortcut overload" warnings (see
  open question Q5 — fix here or fold into this effort).

### 1.4 Bespoke hardcoded shortcuts bypassing everything

- `ImConMainView` menu `QAction`s: `Ctrl+P` (load params), `Ctrl+Shift+S` /
  `Ctrl+Shift+L` (save/load widget states).
- `LeicaStandController`: `F2` hardcoded `QShortcut` (`WindowShortcut`).
- Local `keyPressEvent` handlers in `SLMDisplay`, `BSC203Widget`, several scan
  widgets, and `LaserWidget` — invisible to any registry, not rebindable.

### 1.5 Cross-cutting problems

1. No single source of truth: four mechanisms, three of them hardcoded.
2. No stable action identity: the central registry keys by method name and can
   raise on collisions; there is no namespaced action ID to bind config against.
3. Inconsistent Qt context/lifecycle: `QAction` (WindowShortcut) vs
   `QShortcut` (ApplicationShortcut) vs `keyPressEvent`, with at least one
   disposal bug.
4. Config can only tweak the positioner modifier group; nothing else is
   user-rebindable except per-mode shortcuts.
5. Collection scope is widgets-only, silently dropping manager-level shortcuts.

---

## 2. Target design

One registry, stable action IDs, code-level defaults, config overrides, a single
manager that owns Qt binding/lifecycle/conflict-handling.

### 2.1 Stable action IDs + defaults

- Every shortcut-able action gets a **stable, namespaced action ID**, e.g.
  `positioner.X.plus`, `settings.nextDetector`, `image.updateLevels`,
  `leica.toggleMode`, `app.saveWidgetStates`, `mode.<modeName>`.
- `@shortcut` is extended (or wrapped) to declare `(actionId, defaultKey, name)`.
  The default key is the code-level fallback; the action ID is the binding key
  for config. Keep backward compatibility for existing decorator call sites.

### 2.2 Config override layer

- Add a `shortcuts` mapping to the config:
  `{ actionId: keySequence | [keySequence, ...] | null }`
  where `null` disables the action's binding and a list allows multiple
  sequences. Validate each via `QKeySequence`; report unknown action IDs and
  conflicts at load.
- Storage decision in Phase 0 (Q1): the **setup config** (consistent with
  `shortcutModifier`), and/or a per-user override file (like
  `imcontrol_setup_mode_settings.json`). Recommended: setup config as the base,
  optional user-override file layered on top.

### 2.3 Central `ShortcutManager`

A single owner that:
- collects default-decorated actions across widgets **and** controllers **and**
  managers (fixing the widgets-only scope),
- merges config overrides over defaults to compute the effective binding per
  action ID,
- creates the Qt objects with a consistent, documented context, and **disposes
  them correctly** (no `setParent(None)` leak),
- detects and reports conflicts (two action IDs → same sequence) instead of
  emitting Qt "ambiguous shortcut" warnings,
- builds the `&Shortcuts` menu from the effective bindings,
- exposes an API to query/rebind/reset at runtime (for the editor in Phase 5).

### 2.4 Migrating the outliers

Bring menu actions, LeicaStand F2, the positioner jog, and (optionally)
setup-mode shortcuts under the manager. Convert ad-hoc `keyPressEvent` handlers
where they represent user-facing actions worth rebinding; leave purely-local
widget key handling (e.g. canvas interactions) alone.

---

## 3. Open questions / decisions (Phase 0 — for review)

- **Q1 — Config location.** Setup config `shortcuts` section, a separate
  per-user keybindings file, or both (config base + user override)?
  Recommendation: both, with user override winning.
- **Q2 — Action ID scheme.** Confirm the namespacing convention
  (`<area>.<thing>.<verb>`) and how per-device actions are namespaced
  (by device name, e.g. `positioner.<name>.X.plus`, vs by role).
- **Q3 — Positioner jog model.** Replace the two-group `shortcutModifier` hack
  with arbitrary per-action bindings (keeping `shortcutModifier` as a
  back-compat alias that expands to default bindings)? Or keep groups and just
  make the group keys configurable?
- **Q4 — Dead GRBL manager shortcuts.** Were manager-level `@shortcut`s
  intentionally dropped, or should the collector include managers? (Affects
  whether stage jog can be defined at the manager level.)
- **Q5 — Setup-mode shortcuts.** Fold mode-switch shortcuts into the unified
  manager (one binding table) or keep them per-mode but route creation/disposal
  through the manager (fixing the leak either way)?
- **Q6 — Conflict policy.** On a config conflict (two actions, same key): hard
  error at load, last-wins, or warn-and-disable-both? Recommendation:
  warn + keep the higher-priority/explicit binding, disable the other.

---

## 4. Execution plan

Phases are dependency-ordered. Within a phase, task units are self-contained
(separate files, minimal shared edits) for separate sequential agent sessions
(shared checkout — one run at a time, merge before next, per the established
workflow).

### Phase 0 — Decisions + spec (single owner, blocking)
- Resolve Q1–Q6. Produce `docs/shortcuts_contract_spec.md`: the extended
  `@shortcut`/action-ID contract, the config schema, the ShortcutManager API,
  conflict policy, and the migration list. Phase 1+ consume it verbatim.

### Phase 1 — Action IDs + widened collection (single owner, blocking)
- Extend the decorator to carry stable action IDs (keep current call sites
  working). Replace method-name keying with action-ID keying in
  `generateShortcuts` (or its successor).
- Widen collection to controllers + managers (or relocate manager shortcuts) so
  nothing is silently dropped (resolves Q4). No user-visible behavior change;
  defaults unchanged.

### Phase 2 — ShortcutManager + config schema (single owner, blocking)
- Add `ShortcutManager` (own the build/merge/conflict/dispose lifecycle) and the
  `shortcuts` config schema + validation in `SetupInfo`.
- Route the `&Shortcuts` menu through the manager; effective bindings =
  defaults + config overrides. Add tests: override applies, unknown id warns,
  conflict handled per policy, disposal leaves no stale binding.

### Phase 3 — Migrate outliers (independent task units)
- 3a: menu actions (Ctrl+P, Ctrl+Shift+S/L) → action IDs + manager.
- 3b: LeicaStand F2 → action ID + manager.
- 3c: positioner jog → per-action bindings via manager, `shortcutModifier`
  back-compat alias (resolves Q3).
- 3d: setup-mode shortcuts → through the manager (resolves Q5; fixes the
  disposal leak as part of the unified lifecycle).
- 3e (optional): selected `keyPressEvent` handlers → rebindable actions.
These depend only on Phase 2 and are mutually independent.

### Phase 4 — Editor + docs
- A view/edit/reset dialog that reads effective bindings, edits them with live
  conflict feedback, and writes the config/user-override file. User docs for the
  `shortcuts` config section and the default binding table.

---

## 5. Suggested agent task breakdown

Self-contained units, dependency-ordered:
1. `phase0_shortcuts_contract_spec` — decisions Q1–Q6 + spec doc. (blocking)
2. `phase1_action_ids_and_collection` — decorator action IDs + widen collection. (blocking)
3. `phase2_shortcut_manager_and_config` — ShortcutManager + config schema + menu rewire + tests. (blocking)
4. `phase3a_menu_actions_migrate`
5. `phase3b_leicastand_migrate`
6. `phase3c_positioner_jog_migrate`
7. `phase3d_setup_mode_shortcuts_migrate` (also fixes the QShortcut disposal leak)
8. `phase3e_keypress_handlers_migrate` (optional)
9. `phase4_shortcut_editor_and_docs`

Tasks 4–8 (Phase 3) are the independent, fan-out-friendly block; 1–3 are hard
prerequisites and 9 depends on Phase 3.

Note (lessons carried from the state-persistence effort): review every agent
task before merge — verify any file:line citations (agents fabricate them),
watch for scope over-reach, run the full unit suite in the openhands-clean venv,
and merge `--no-ff` into the working branch one task at a time.

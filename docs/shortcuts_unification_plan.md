# Keyboard shortcuts unification — audit and plan

> **STATUS: COMPLETE (2026-06-20).** Phases 0–4 implemented and merged into
> `codex/testalab-scandev-port` (see `docs/agent_tasks/shortcuts_phase*.md`).
> Shortcuts are now config-driven through one `ShortcutManager`: every action has
> a stable ID, the setup-config `shortcuts` map rebinds/disables anything (with
> conflict detection), and a "Configure Shortcuts…" editor dialog edits/persists
> bindings. All outliers (File-menu actions, LeicaStand F2, positioner jog,
> setup-mode shortcuts) migrated; the "Ambiguous shortcut overload" disposal bug
> is fixed. User docs: `gui.rst` (Keyboard shortcuts) and `setupinfo-reference.rst`
> (`shortcuts` field). Optional/skipped: Phase 3e (keyPressEvent handlers), Phase
> 3f (GRBL manager-jog decision).

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
- Currently decorated in `imcontrol`: `PositionerWidget` (Ctrl+Arrows,
  Ctrl+Shift+Arrows, Ctrl+Y/Ctrl+A for Z), `SettingsWidget` (Ctrl+N, next
  detector), `ImageWidget` (Ctrl+U, update levels), `ViewWidget` (Ctrl+L,
  live view), and `RecordingWidget` (Ctrl+R, record).
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
- Local `keyPressEvent`/`keyReleaseEvent` handlers in `SLMDisplay` and
  `BSC203Widget` — invisible to any registry, not rebindable. `BSC203Widget`
  is press/release motion control, so it is not the same shape as one-shot
  shortcut activation.
- Outside `imcontrol`, there are also hardcoded module-local shortcuts (for
  example ImProcess and ImScripting menu actions). Decide in Phase 0 whether
  this effort is strictly `imcontrol`-scoped or whether those modules get their
  own manager/adapters later.

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
- The action contract includes more than a key:
  - `actionId`
  - display name
  - default key sequence(s)
  - scope/context (`ApplicationShortcut`, `WindowShortcut`, widget-local, or
    press/release handler)
  - owning widget/window, when needed for Qt binding
  - enabled predicate / availability metadata
  - activation source metadata, where callbacks need to distinguish shortcut
    activation from menu/button activation
- `@shortcut` is extended (or wrapped) to declare `(actionId, defaultKey, name,
  metadata...)`. The default key is the code-level fallback; the action ID is
  the binding key for config. Keep backward compatibility for existing decorator
  call sites.

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
- If the user-override file is adopted, the `ShortcutManager` owns the merge in
  Phase 2: code defaults < setup config < user override. The editor in Phase 4
  only writes through that already-tested persistence path.

### 2.3 Central `ShortcutManager`

A single owner that:
- collects default-decorated actions across widgets **and** controllers **and**
  managers (fixing the widgets-only scope), but can catalog an action without
  binding it yet,
- merges config overrides over defaults to compute the effective binding per
  action ID,
- creates the Qt objects with each action's documented scope/context, and
  **disposes them correctly** (no `setParent(None)` leak),
- detects and reports conflicts (two action IDs → same sequence) instead of
  emitting Qt "ambiguous shortcut" warnings,
- builds the `&Shortcuts` menu from the effective bindings,
- exposes an API to query/rebind/reset at runtime (for the editor in Phase 4).

### 2.4 Migrating the outliers

Bring menu actions, LeicaStand F2, the positioner jog, and (optionally)
setup-mode shortcuts under the manager. Convert ad-hoc `keyPressEvent` handlers
where they represent user-facing actions worth rebinding; leave purely-local
widget key handling (e.g. canvas interactions) alone.

Important migration guard: widening discovery must not implicitly activate
currently-dead bindings. Manager-level shortcuts such as the GRBL arrow-key jog
must be cataloged first and only enabled during an explicit migration step (or
behind an explicit config/default decision).

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
- **Q7 — Scope boundary.** Is this effort `imcontrol` only, or should it also
  define shared machinery for ImProcess/ImScripting hardcoded menu shortcuts?
- **Q8 — Action metadata.** Confirm the required per-action metadata: Qt
  context/scope, owner widget/window, enabled predicate, and activation source
  payload.
- **Q9 — Setup-mode compatibility.** Preserve the current setup-mode behavior
  where duplicate mode shortcuts prompt to replace the other mode, and shortcut
  application passes `source="shortcut"` for safety confirmation logic. Decide
  how mode-vs-global conflicts behave.

---

## 4. Execution plan

Phases are dependency-ordered. Within a phase, task units are self-contained
(separate files, minimal shared edits) for separate sequential agent sessions
(shared checkout — one run at a time, merge before next, per the established
workflow).

### Phase 0 — Decisions + spec (single owner, blocking)
- Resolve Q1–Q9. Produce `docs/shortcuts_contract_spec.md`: the extended
  `@shortcut`/action-ID contract, the required action metadata, the config and
  user-override schema/merge order, the ShortcutManager API, conflict policy,
  setup-mode compatibility rules, and the migration list. Phase 1+ consume it
  verbatim.
- Produce an authoritative inventory table with action ID, current source,
  default key, scope/context, owner, enabled predicate, migration phase, and
  whether the action is initially bound or only cataloged.

### Phase 1 — Action IDs + catalog-only widened collection (single owner, blocking)
- Extend the decorator to carry stable action IDs (keep current call sites
  working). Replace method-name keying with action-ID keying in
  `generateShortcuts` (or its successor).
- Widen collection to controllers + managers (or relocate manager shortcuts) in
  catalog-only mode so nothing is silently dropped (resolves Q4) while keeping
  the actually-bound default set unchanged. No user-visible behavior change;
  currently-dead manager bindings remain unbound unless explicitly enabled by
  the migration inventory.

### Phase 2 — ShortcutManager + config schema (single owner, blocking)
- Add `ShortcutManager` (own the build/merge/conflict/dispose lifecycle) and the
  `shortcuts` config schema + validation in `SetupInfo`.
- If Q1 chooses user overrides, add the user-override file loader/writer and
  tested merge order here, not in the editor phase.
- Route the `&Shortcuts` menu through the manager; effective bindings =
  defaults + setup config overrides + optional user overrides. Add tests:
  override applies, unknown id warns, conflict handled per policy, user
  override wins setup config when enabled, disposal leaves no stale binding.

### Phase 3 — Migrate outliers (independent task units)
- 3a: menu actions (Ctrl+P, Ctrl+Shift+S/L) → action IDs + manager.
- 3b: LeicaStand F2 → action ID + manager.
- 3c: positioner jog → per-action bindings via manager, `shortcutModifier`
  back-compat alias (resolves Q3).
- 3d: setup-mode shortcuts → through the manager (resolves Q5; fixes the
  disposal leak as part of the unified lifecycle; preserves `source="shortcut"`
  and existing duplicate-mode prompt semantics unless Q9 decides otherwise).
- 3e (optional): selected `keyPressEvent` handlers → rebindable actions. Treat
  press/release motion controls as a separate action shape; do not convert them
  to one-shot `activated` callbacks by accident.
- 3f (optional): GRBL manager shortcuts → explicit manager-level migration or
  removal if they are confirmed obsolete.
These depend only on Phase 2 and are mutually independent.

### Phase 4 — Editor + docs
- A view/edit/reset dialog that reads effective bindings, edits them with live
  conflict feedback, and writes the config/user-override file. User docs for the
  `shortcuts` config section and the default binding table.

---

## 5. Suggested agent task breakdown

Self-contained units, dependency-ordered:
1. `phase0_shortcuts_contract_spec` — decisions Q1–Q9 + spec doc + inventory table. (blocking)
2. `phase1_action_ids_and_catalog` — decorator action IDs + widened catalog-only collection. (blocking)
3. `phase2_shortcut_manager_and_config` — ShortcutManager + config schema + menu rewire + tests. (blocking)
4. `phase3a_menu_actions_migrate`
5. `phase3b_leicastand_migrate`
6. `phase3c_positioner_jog_migrate`
7. `phase3d_setup_mode_shortcuts_migrate` (also fixes the QShortcut disposal leak)
8. `phase3e_keypress_handlers_migrate` (optional)
9. `phase3f_grbl_manager_shortcuts_decide_or_migrate` (optional)
10. `phase4_shortcut_editor_and_docs`

Tasks 4–9 (Phase 3) are the independent, fan-out-friendly block; 1–3 are hard
prerequisites and 10 depends on Phase 3.

Note (lessons carried from the state-persistence effort): review every agent
task before merge — verify any file:line citations (agents fabricate them),
watch for scope over-reach, run the full unit suite in the openhands-clean venv,
and merge `--no-ff` into the working branch one task at a time.
